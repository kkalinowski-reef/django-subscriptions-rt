from datetime import timedelta
from itertools import chain
from typing import Iterable

from django.conf import settings
from django.db.models import Prefetch
from django.utils.timezone import now
from more_itertools import first, pairwise, spy

from .defaults import DEFAULT_SUBSCRIPTIONS_OFFLINE_CHARGE_ATTEMPTS_SCHEDULE
from .exceptions import PaymentError, ProlongationImpossible
from .models import Subscription, SubscriptionPayment
from .providers import get_provider


def charge_recurring_subscriptions(
    charge_attempts_schedule: Iterable[timedelta] = getattr(
        settings,
        'SUBSCRIPTIONS_OFFLINE_CHARGE_ATTEMPTS_SCHEDULE',
        DEFAULT_SUBSCRIPTIONS_OFFLINE_CHARGE_ATTEMPTS_SCHEDULE,
    ),
):
    charge_attempts_schedule = sorted(
        delta for delta in charge_attempts_schedule if delta < timedelta(0)
    )

    expiring_subscriptions = Subscription.get_expiring(
        within=abs(charge_attempts_schedule[0]),
    ).prefetch_related(
        Prefetch('payments', queryset=SubscriptionPayment.objects.order_by('created')),
    )

    now_ = now()
    for subscription in expiring_subscriptions:
        expiration_date = next(subscription.iter_charge_dates(since=now_))
        charge_dates = (expiration_date + delta for delta in charge_attempts_schedule)
        (first_charge_date,), charge_dates = spy(charge_dates)
        charge_periods = pairwise(chain(charge_dates, (expiration_date,)))

        charge_period = first(
            period for period in charge_periods
            if period[0] <= now_ < period[1]
        )

        if subscription.payments.filter(
            created__gte=charge_period[0],
            created__lt=charge_period[1],
        ).exists():
            continue  # don't try to charge one more time in this period

        try:
            subscription.prolong()  # try extending end date of subscription
        except ProlongationImpossible:
            continue  # TODO: send email to user

        try:
            subscription.charge_offline()
            subscription.send_successful_charge_email()
        except PaymentError:
            subscription.send_failed_charge_email()
            continue

        subscription.save()


def check_unfinished_payments(within: timedelta = timedelta(hours=12)):
    """
    Reverse-check payment status: if payment webhook didn't pass through
    for some reason, ask payment provider about payment status, and
    update SubscriptionPayment status if needed.
    """
    now_ = now()
    unfinished_payments = SubscriptionPayment.objects.filter(
        created__gte=now_ - within,
        status=SubscriptionPayment.Status.PENDING,
    )

    codenames = set(unfinished_payments.order_by('provider_codename').values_list('provider_codename', flat=True))

    for codename in codenames:
        get_provider(codename).check_payments(
            unfinished_payments.filter(provider_codename=codename)
        )


# TODO: check for concurrency issues, probably add transactions
