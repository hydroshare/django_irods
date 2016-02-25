# Create your views here.
from uuid import uuid4
import os
import mimetypes

from rest_framework.decorators import api_view

from django_irods import icommands
from django_irods.storage import IrodsStorage
from django.conf import settings
from django.http import HttpResponse, FileResponse

from hs_core.views.utils import authorize, Action_To_Authorize
from hs_core.hydroshare.hs_bagit import create_bag_by_irods
from . import models as m
from .icommands import Session, GLOBAL_SESSION


@api_view(['GET'])
def download(request, path, *args, **kwargs):
    
    split_path_strs = path.split('/')
    if split_path_strs[0] == 'bags':
        res_id = os.path.splitext(split_path_strs[1])[0]
    else:
        res_id = split_path_strs[0]
    _, authorized, _ = authorize(request, res_id, needed_permission=Action_To_Authorize.VIEW_RESOURCE,
                                 raises_exception=False)
    if not authorized:
        response = HttpResponse()
        response.content = "<h1>You do not have permission to download this resource!</h1>"
        return response

    if 'environment' in kwargs:
        environment = int(kwargs['environment'])
        environment = m.RodsEnvironment.objects.get(pk=environment)
        session = Session("/tmp/django_irods", settings.IRODS_ICOMMANDS_PATH, session_id=uuid4())
        session.create_environment(environment)
        session.run('iinit', None, environment.auth)
    elif getattr(settings, 'IRODS_GLOBAL_SESSION', False):
        session = GLOBAL_SESSION
    elif icommands.ACTIVE_SESSION:
        session = icommands.ACTIVE_SESSION
    else:
        raise KeyError('settings must have IRODS_GLOBAL_SESSION set if there is no environment object')

    # do on-demand bag creation
    istorage = IrodsStorage()
    bag_modified = "false"
    # needs to check whether res_id collection exists before getting/setting AVU on it to accommodate the case
    # where the very same resource gets deleted by another request when it is getting downloaded
    if istorage.exists(res_id):
        bag_modified = istorage.getAVU(res_id, 'bag_modified')
    if bag_modified == "true":
        create_bag_by_irods(res_id, istorage)
        if istorage.exists(res_id):
            istorage.setAVU(res_id, 'bag_modified', "false")

    # obtain mime_type to set content_type
    mtype = 'application-x/octet-stream'
    mime_type = mimetypes.guess_type(path)
    if mime_type[0] is not None:
        mtype = mime_type[0]

    # retrieve file size to set up Content-Length header
    stdout = session.run("ils", None, "-l", path)[0].split()
    flen = int(stdout[3])

    options = ('-',) # we're redirecting to stdout.
    proc = session.run_safe('iget', None, path, *options)
    response = FileResponse(proc.stdout, content_type=mtype)
    response['Content-Disposition'] = 'attachment; filename="{name}"'.format(name=path.split('/')[-1])
    response['Content-Length'] = flen
    return response


def list(request, *args, **kwargs):
    if 'environment' in kwargs:
        environment = int(kwargs['environment'])
        environment = m.RodsEnvironment.objects.get(pk=environment)
        session = Session("/tmp/django_irods", settings.IRODS_ICOMMANDS_PATH, session_id=uuid4())
        session.create_environment(environment)
        session.run('iinit', None, environment.auth)
    elif getattr(settings, 'IRODS_GLOBAL_SESSION', False):
        session = GLOBAL_SESSION
    else:
        raise KeyError('settings must have IRODS_GLOBAL_SESSION set if there is no environment object')

    options = ('-',) # we're redirecting to stdout.

    proc = session.run_safe('ils', None, *options)
    response = HttpResponse(proc.stdout)
    return response
