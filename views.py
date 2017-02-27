from uuid import uuid4

from django_irods.storage import IrodsStorage
from django.conf import settings
from django.http import StreamingHttpResponse

from hs_core.views.utils import authorize, ACTION_TO_AUTHORIZE
from hs_core.signals import pre_download_file, pre_check_bag_flag


def download(request, irods_backend_slug, path, *args, **kwargs):
   filename = [x for x in path.split('/') if x][-1] #ignore trailing /s
   path = '/{0}'.format(path)
   storage = IrodsStorage(irods_backend=irods_backend_slug)
   response = StreamingHttpResponse(storage.open(path, 'r'), storage.chunk_size)
   response['Content-Length'] = storage.size(path)
   response['Content-Disposition'] = "attachment; filename=%s" % filename
   return response
