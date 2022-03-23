# Generated by Django 4.0.2 on 2022-02-18 08:45

from django.db import migrations, models
import djmoney.models.fields


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0002_alter_subscription_begin'),
    ]

    operations = [
        migrations.AlterField(
            model_name='plan',
            name='charge_amount',
            field=djmoney.models.fields.MoneyField(blank=True, decimal_places=2, default_currency='USD', max_digits=14, null=True),
        ),
        migrations.AlterField(
            model_name='usage',
            name='datetime',
            field=models.DateTimeField(blank=True),
        ),
    ]