from django.conf import settings
from django.core.files.storage import Storage
from django.core.urlresolvers import reverse
from django.utils.deconstruct import deconstructible

from irods.session import iRODSSession
from irods.exception import DataObjectDoesNotExist


@deconstructible
class IrodsStorage(Storage):
    def __init__(self, irods_backend=None):
        if not irods_backend:
            backend = settings.IRODS_BACKENDS['default']
        else:
            backend = settings.IRODS_BACKENDS[irods_backend]
        self.session = iRODSSession(
                            host=backend.get('HOST', 'localhost'),
                            port=backend.get('PORT', 1247),
                            user=backend['USER'],
                            password=backend['PASSWORD'],
                            zone=backend['ZONE'])
        # self.base_collection = self.session.collections.get(backend['BASE_COLLECTION'])
        self.chunk_size = backend.get('CHUNK_SIZE', 524288)

    def path(self, name):
        return name

    def _open(self, name, mode='r'):
        data_object = self.session.data_objects.get(self.path(name))
        return data_object.open(mode, buffer_size=self.chunk_size)

    def _save(self, name, content):
        data_object = self.session.data_objects.get(self.path(name))
        return data_object.close()

    def delete(self, name):
        self.session.data_objects.unlink(self.data_object.path)

    def exists(self, name):
        try:
            self.session.data_objects.get(self.path())
        except DataObjectDoesNotExist:
            return False
        return True

    def listdir(self, path):
        collection = self.session.collections.get(path)
        return collection.subcollections, collection.data_objects

    def size(self, name):
        data_object = self.session.data_objects.get(self.path(name))
        return data_object.size

    def url(self, name):
        return reverse('django_irods.views.download', kwargs={'path': name})
