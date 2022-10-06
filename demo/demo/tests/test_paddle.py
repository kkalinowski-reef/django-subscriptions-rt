from datetime import datetime, timedelta

from django.test.client import MULTIPART_CONTENT
from django.utils.timezone import now
from freezegun import freeze_time
from subscriptions.models import Subscription, SubscriptionPayment
from subscriptions.providers import get_provider
from subscriptions.providers.paddle import PaddleProvider
from subscriptions.tasks import check_unfinished_payments
from tenacity import Retrying, TryAgain, stop_after_attempt, wait_fixed


def test_provider(paddle):
    assert isinstance(get_provider(), PaddleProvider)


def test_payment_flow(paddle, user_client, plan, card_number):
    response = user_client.post('/api/subscribe/', {'plan': plan.id})
    assert response.status_code == 200, response.content

    result = response.json()

    redirect_url = result.pop('redirect_url')
    assert 'paddle.com' in redirect_url

    payment = SubscriptionPayment.objects.latest()
    assert result == {
        'plan': plan.id,
        'quantity': 1,
        'background_charge_succeeded': False,
        'payment_id': payment.id,
    }

    assert 'payment_url' in payment.metadata

    # TODO: automate this
    input(f'Use card {card_number} to pay here: {redirect_url}\nThen press Enter')

    # ensure that status didn't change because webhook didn't go through
    assert payment.status == SubscriptionPayment.Status.PENDING

    # ---- test_payment_status_endpoint_get ----
    response = user_client.get(f'/api/payments/{payment.id}/')
    assert response.status_code == 200, response.content

    result = response.json()
    assert result == {
        'id': payment.id,
        'status': 'pending',
        'quantity': 1,
        'subscription': None,
    }

    # ---- test_payment_status_endpoint_post ----
    for attempt in Retrying(wait=wait_fixed(2), stop=stop_after_attempt(10)):
        with attempt:
            response = user_client.post(f'/api/payments/{payment.id}/')
            assert response.status_code == 200, response.content
            result = response.json()
            if result['status'] != 'completed':
                raise TryAgain()

    payment = SubscriptionPayment.objects.get(pk=payment.pk)

    assert result == {
        'id': payment.id,
        'status': 'completed',
        'quantity': 1,
        'subscription': {
            'id': payment.subscription.id,
            'quantity': 1,
            'start': payment.subscription.start.isoformat().replace('+00:00', 'Z'),
            'end': payment.subscription.end.isoformat().replace('+00:00', 'Z'),
            'plan': {
                'charge_amount': 100,
                'charge_amount_currency': 'USD',
                'charge_period': {'days': 30},
                'codename': 'plan',
                'id': 1,
                'is_recurring': True,
                'max_duration': {'days': 120},
                'metadata': {'this': 'that'},
                'name': 'Plan',
            },
        },
    }

    # ---- test_check_unfinished_payments ----
    payment = SubscriptionPayment.objects.latest()
    payment.status = SubscriptionPayment.Status.PENDING
    payment.save()

    check_unfinished_payments(within=timedelta(hours=1))
    payment = SubscriptionPayment.objects.latest()
    assert payment.status == SubscriptionPayment.Status.COMPLETED

    # ---- test_charge_offline ----
    assert 'subscription_id' in payment.metadata
    payment.subscription.charge_offline()
    assert SubscriptionPayment.objects.count() == 2

    last_payment = SubscriptionPayment.objects.latest()
    subscription = last_payment.subscription

    assert last_payment.provider_codename == payment.provider_codename
    provider = get_provider(last_payment.provider_codename)
    assert last_payment.amount == provider.get_amount(
        user=last_payment.user,
        plan=plan,
        quantity=last_payment.quantity,
    )
    assert last_payment.quantity == subscription.quantity
    assert last_payment.user == subscription.user
    assert last_payment.subscription == subscription
    assert last_payment.plan == plan

    # check subsequent offline charge
    payment.subscription.charge_offline()


def test_webhook(paddle, client, user_client, paddle_unconfirmed_payment, paddle_webhook_payload):
    response = user_client.get('/api/subscriptions/')
    assert response.status_code == 200, response.content
    assert len(response.json()) == 0

    webhook_time = now() + timedelta(hours=2)
    with freeze_time(webhook_time):
        response = client.post('/api/webhook/paddle/', paddle_webhook_payload)
        assert response.status_code == 200, response.content

    with freeze_time(webhook_time + timedelta(hours=1)):
        response = user_client.get('/api/subscriptions/')
        assert response.status_code == 200, response.content
        subscriptions = response.json()
        assert len(subscriptions) == 1

        # check that subscription started when webhook arrived
        subscription = subscriptions[0]
        start = datetime.fromisoformat(subscription['start'].replace('Z', '+00:00'))
        assert start - webhook_time < timedelta(seconds=10)

        # check that subscription lasts as much as stated in plan description
        end = datetime.fromisoformat(subscription['end'].replace('Z', '+00:00'))
        assert start + paddle_unconfirmed_payment.plan.charge_period == end


def test_webhook_idempotence(paddle, client, paddle_unconfirmed_payment, paddle_webhook_payload):
    assert not Subscription.objects.all().exists()

    response = client.post('/api/webhook/paddle/', paddle_webhook_payload)
    assert response.status_code == 200, response.content
    start_old, end_old = Subscription.objects.values_list('start', 'end').latest()

    response = client.post('/api/webhook/paddle/', paddle_webhook_payload)
    assert response.status_code == 200, response.content
    start_new, end_new = Subscription.objects.values_list('start', 'end').latest()

    assert start_old == start_new
    assert end_old == end_new


def test_webhook_payload_as_form_data(paddle, client, paddle_unconfirmed_payment, paddle_webhook_payload):
    assert not Subscription.objects.all().exists()

    response = client.post('/api/webhook/paddle/', paddle_webhook_payload, content_type=MULTIPART_CONTENT)
    assert response.status_code == 200, response.content

    payment = SubscriptionPayment.objects.get(pk=paddle_unconfirmed_payment.pk)
    assert not isinstance(payment.metadata['subscription_id'], list)
