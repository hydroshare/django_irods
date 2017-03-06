#################################################
django_irods - A Django Storage Backend for iRODS
#################################################

This module provides a [Django storage
backend](https://docs.djangoproject.com/en/1.10/topics/files/#file-storage) for
the [iRODS](https://irods.org/) distributed storage system.

Usage
-----

``settings.py:``

```python
IRODS_BACKENDS = {
    "default": {
        "HOST": "irods.storage.system.com",
        "PORT": 1247,
        "USER": "irods_username",
        "PASSWORD": "irods_pw",
        "ZONE": "irods_zone",
    },
    "other_irods": {
        "HOST": "backups.storage.system.com",
        "PORT": 1247,
        "USER": "other_user",
        "PASSWORD": "other_pw",
        "ZONE": "irods_zone2",
        "CHUNK_SIZE": 2048,
    }
}
```

``models.py:``

```python
from django.db import models
from django_irods.storage import IrodsStorage

class Car(Models.model):
    #default iRODS backend
    photo = FileField(storage=IrodsStorage())

    #second iRODS backend
    photo2 = FileField(storage=IrodsStorage(backend="other_irods"))
```
