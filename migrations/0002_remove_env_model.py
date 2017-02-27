# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('django_irods', '0001_initial'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='rodsenvironment',
            name='owner',
        ),
        migrations.DeleteModel(
            name='RodsEnvironment',
        ),
    ]
