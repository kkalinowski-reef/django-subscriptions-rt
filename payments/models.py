from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from functools import reduce
from typing import Iterator, Optional

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Index, QuerySet, UniqueConstraint
from django.utils.timezone import now

from .fields import MoneyField

#
#  |--------subscription-------------------------------------------->
#  begin             (subscription duration)                end or inf
#
#  |-----------------------------|---------------------------|------>
#  charge   (charge period)    charge                      charge
#
#  |------------------------------x
#  quota   (quota lifetime)       quota burned
#
#  (quota recharge period) |------------------------------x
#
#  (quota recharge period) (quota recharge period) |----------------->


INFINITY = timedelta(days=365 * 1000)


class Plan(models.Model):
    codename = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    charge_amount = MoneyField()
    charge_period = models.DurationField(blank=True, help_text='leave blank for one-time charge')
    subscription_duration = models.DurationField(blank=True, help_text='leave blank to make it an infinite subscription')
    is_enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['codename'], name='unique_plan'),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        self.charge_period = self.charge_period or INFINITY
        self.subscription_duration = self.subscription_duration or INFINITY
        return super().save(*args, **kwargs)


class SubscriptionManager(models.Manager):
    def active(self):
        return self.filter(end__gte=now())


class Subscription(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='subscriptions')
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name='subscriptions')
    # amount = MoneyField()  # should match plan.charge_amount
    begin = models.DateTimeField(blank=True)
    end = models.DateTimeField(blank=True)

    objects = SubscriptionManager()

    def __str__(self) -> str:
        return f'{self.user} @ {self.plan}'

    def save(self, *args, **kwargs):
        self.begin = self.begin or now()
        self.end = self.end or (self.begin + self.plan.subscription_duration)
        return super().save(*args, **kwargs)

    def stop(self):
        self.end = now()
        self.save(update_fields=['end'])

    @classmethod
    def get_expiring(cls, within: timedelta) -> QuerySet:
        return cls.objects.active().filter(end__lte=now() + within)


class Resource(models.Model):
    codename = models.CharField(max_length=255)
    units = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['codename'], name='unique_resource'),
        ]

    def __str__(self) -> str:
        return self.codename


@dataclass
class Event:
    class Type(Enum):
        RECHARGE = auto()
        BURN = auto()
        USAGE = auto()

    datetime: datetime
    resource: Resource
    type: Type
    value: int


class Quota(models.Model):
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name='quotas')
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name='quotas')
    limit = models.PositiveIntegerField()
    recharge_period = models.DurationField(blank=True, help_text='leave blank for recharging only after each subscription prolongation (charge)')
    burns_in = models.DurationField(blank=True, help_text='leave blank to burn each recharge period')

    class Meta:
        constraints = [
            UniqueConstraint(fields=['plan', 'resource'], name='unique_quota'),
        ]

    def __str__(self) -> str:
        return f'{self.resource}: {self.limit:,}{self.resource.units}/{self.recharge_period}'

    def save(self, *args, **kwargs):
        self.recharge_period = self.recharge_period or self.plan.charge_period
        self.burns_in = self.burns_in or self.recharge_period
        return super().save(*args, **kwargs)

    @classmethod
    def iter_events(cls, user: AbstractUser, since: Optional[datetime] = None) -> Iterator[Event]:
        active_subscriptions = Subscription.objects.active().filter(user=user)
        since = since or active_subscriptions.values_list('begin').order_by('begin').first()
        now_ = now()

        resources_with_quota = set()

        for subscription in active_subscriptions.select_related('plan__quotas'):
            for quota in subscription.plan.quotas.all():
                resources_with_quota.add(quota.resource)

                i = 0
                while True:
                    recharge_time = subscription.begin + i * quota.recharge_period
                    if recharge_time > now_:
                        break

                    if recharge_time >= since:
                        yield Event(
                            datetime=recharge_time,
                            resource=quota.resource,
                            type=Event.Type.RECHARGE,
                            value=quota.limit,
                        )

                    burn_time = recharge_time + quota.burns_in
                    if since <= burn_time <= now_:
                        yield Event(
                            datetime=burn_time,
                            resource=quota.resource,
                            type=Event.Type.BURN,
                            value=-quota.limit,
                        )

                    i += 1

        for resource in resources_with_quota:
            for usage_time, amount in Usage.objects.filter(user=user, resource=resource, datetime__gte=since, datetime__lte=now_).order_by('datetime').values_list('datetime', 'amount'):
                yield Event(
                    datetime=usage_time,
                    resource=resource,
                    type=Event.Type.USAGE,
                    value=-amount,
                )

    @classmethod
    def calculate_remaining(cls, user: AbstractUser, since: Optional[datetime] = None, initial: Optional[dict['Resource', int]] = None) -> dict[Resource, int]:
        initial = initial or {}

        events: dict[Resource, list[Event]] = defaultdict(list)
        for event in cls.iter_events(user, since=since):
            events[event.resource].append(event)

        for resource in events:
            events[resource].sort()

        return {
            resource: reduce(lambda remains, event: max(0, remains + event.value), event_list, initial.get(resource, 0))
            for resource, event_list in events.items()
        }


class Usage(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='usages')
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name='usages')
    amount = models.PositiveIntegerField(default=1)
    datetime = models.DateTimeField(blank=True)

    class Meta:
        indexes = [
            Index(fields=['user', 'resource']),
        ]

    def __str__(self) -> str:
        return f'{self.amount:,}{self.resource.units} {self.resource}'

    def save(self, *args, **kwargs):
        self.datetime = self.datetime or now()
        return super().save(*args, **kwargs)
