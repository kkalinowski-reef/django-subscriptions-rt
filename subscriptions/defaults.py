from datetime import timedelta

from django.urls import reverse_lazy

DEFAULT_SUBSCRIPTIONS_PAYMENT_PROVIDERS = [
    'subscriptions.providers.dummy.DummyProvider',
]
DEFAULT_SUBSCRIPTIONS_SUCCESS_URL = reverse_lazy('plan_subscription_success')

DEFAULT_SUBSCRIPTIONS_VALIDATORS = [
    'subscriptions.validators.OnlyEnabledPlans',
    'subscriptions.validators.AtLeastOneRecurringSubscription',
    'subscriptions.validators.SimultaneousRecurringSubscriptions',
]

DEFAULT_SUBSCRIPTIONS_OFFLINE_CHARGE_ATTEMPTS_SCHEDULE = (
    timedelta(days=-3),
    timedelta(days=-2),
    timedelta(days=-1),
    timedelta(hours=-12),
    timedelta(hours=-3),
    timedelta(hours=-1),
)
