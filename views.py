from uuid import uuid4
import os
import json
import mimetypes

from rest_framework.decorators import api_view

from django_irods import icommands
from django_irods.storage import IrodsStorage
from django.conf import settings
from django.http import HttpResponse, FileResponse, HttpResponseRedirect
from django.core.exceptions import PermissionDenied

from hs_core.views.utils import authorize, ACTION_TO_AUTHORIZE
from hs_core.tasks import create_bag_by_irods
from hs_core.hydroshare.resource import FILE_SIZE_LIMIT
from hs_core.signals import pre_download_file, pre_check_bag_flag
from hs_core.hydroshare import check_resource_type
from hs_core.hydroshare.hs_bagit import create_bag_files

from . import models as m
from .icommands import Session, GLOBAL_SESSION


def download(request, path, rest_call=False, use_async=True, *args, **kwargs):
    split_path_strs = path.split('/')
    is_bag_download = False
    if split_path_strs[0] == 'bags':
        res_id = os.path.splitext(split_path_strs[1])[0]
        is_bag_download = True
    else:
        res_id = split_path_strs[0]
    res, authorized, _ = authorize(request, res_id,
                                   needed_permission=ACTION_TO_AUTHORIZE.VIEW_RESOURCE,
                                   raises_exception=False)
    if not authorized:
        response = HttpResponse(status=401)
        content_msg = "You do not have permission to download this resource!"
        if rest_call:
            raise PermissionDenied(content_msg)
        else:
            response.content = "<h1>" + content_msg + "</h1>"
            return response

    if res.resource_federation_path:
        # the resource is stored in federated zone
        istorage = IrodsStorage('federated')
        federated_path = res.resource_federation_path
        path = os.path.join(federated_path, path)
        session = icommands.ACTIVE_SESSION
    else:
        istorage = IrodsStorage()
        federated_path = ''
        if 'environment' in kwargs:
            environment = int(kwargs['environment'])
            environment = m.RodsEnvironment.objects.get(pk=environment)
            session = Session("/tmp/django_irods", settings.IRODS_ICOMMANDS_PATH,
                              session_id=uuid4())
            session.create_environment(environment)
            session.run('iinit', None, environment.auth)
        elif getattr(settings, 'IRODS_GLOBAL_SESSION', False):
            session = GLOBAL_SESSION
        elif icommands.ACTIVE_SESSION:
            session = icommands.ACTIVE_SESSION
        else:
            raise KeyError('settings must have IRODS_GLOBAL_SESSION set '
                           'if there is no environment object')

    resource_cls = check_resource_type(res.resource_type)

    if federated_path:
        res_root = os.path.join(federated_path, res_id)
    else:
        res_root = res_id

    bag_modified = "false"
    metadata_dirty = "false"
    if istorage.exists(res_root):
        bag_modified = istorage.getAVU(res_root, 'bag_modified')
        metadata_dirty = istorage.getAVU(res_root, 'metadata_dirty')

    if is_bag_download:
        # do on-demand bag creation
        # needs to check whether res_id collection exists before getting/setting AVU on it
        # to accommodate the case where the very same resource gets deleted by another request
        # when it is getting downloaded

        # send signal for pre_check_bag_flag
        pre_check_bag_flag.send(sender=resource_cls, resource=res)
        if bag_modified == "true":
            if metadata_dirty == 'true':
                create_bag_files(res, fed_zone_home_path=res.resource_federation_path)
            if use_async:
                # task parameter has to be passed in as a tuple or list, hence (res_id,) is needed
                # Note that since we are using JSON for task parameter serialization, no complex
                # object can be passed as parameters to a celery task
                task = create_bag_by_irods.apply_async((res_id,), countdown=3)
                if rest_call:
                    return HttpResponse(json.dumps({'bag_status': 'Not ready',
                                                    'task_id': task.task_id}),
                                        content_type="application/json")

                request.session['task_id'] = task.task_id
                request.session['download_path'] = request.path
                return HttpResponseRedirect(res.get_absolute_url())
            else:
                ret_status = create_bag_by_irods(res_id, istorage)
                if not ret_status:
                    content_msg = "Bag cannot be created successfully. Check log for details."
                    response = HttpResponse()
                    if rest_call:
                        response.content = content_msg
                    else:
                        response.content = "<h1>" + content_msg + "</h1>"
                    return response

    elif metadata_dirty == 'true':
        if path.endswith("resourcemap.xml") or path.endswith('resourcemetadata.xml'):
            # we need to regenerate the metadata xml files
            create_bag_files(res, fed_zone_home_path=res.resource_federation_path)

    # send signal for pre download file
    download_file_name = split_path_strs[-1]
    pre_download_file.send(sender=resource_cls, resource=res,
                           download_file_name=download_file_name,
                           request=request)

    # obtain mime_type to set content_type
    mtype = 'application-x/octet-stream'
    mime_type = mimetypes.guess_type(path)
    if mime_type[0] is not None:
        mtype = mime_type[0]
    # retrieve file size to set up Content-Length header
    stdout = session.run("ils", None, "-l", path)[0].split()
    flen = int(stdout[3])
    if flen <= FILE_SIZE_LIMIT:
        options = ('-',)  # we're redirecting to stdout.
        proc = session.run_safe('iget', None, path, *options)
        response = FileResponse(proc.stdout, content_type=mtype)
        response['Content-Disposition'] = 'attachment; filename="{name}"'.format(
                                                        name=path.split('/')[-1])
        response['Content-Length'] = flen
        return response
    else:
        content_msg = "File larger than 1GB cannot be downloaded directly via HTTP. " \
                       "Please download the large file via iRODS clients."
        response = HttpResponse(status=403)
        if rest_call:
            response.content = content_msg
        else:
            response.content = "<h1>" + content_msg + "</h1>"
        return response


@api_view(['GET'])
def rest_download(request, path, *args, **kwargs):
    # need to have a separate view function just for REST API call
    return download(request, path, rest_call=True, *args, **kwargs)


def check_task_status(request, task_id=None, *args, **kwargs):
    '''
    A view function to tell the client if the asynchronous create_bag_by_irods()
    task is done and the bag file is ready for download.
    Args:
        request: an ajax request to check for download status
    Returns:
        JSON response to return result from asynchronous task create_bag_by_irods
    '''
    if not task_id:
        task_id = request.POST.get('task_id')
    result = create_bag_by_irods.AsyncResult(task_id)
    if result.ready():
        return HttpResponse(json.dumps({"status": result.get()}),
                            content_type="application/json")
    else:
        return HttpResponse(json.dumps({"status": None}),
                            content_type="application/json")


@api_view(['GET'])
def rest_check_task_status(request, task_id, *args, **kwargs):
    # need to have a separate view function just for REST API call
    return check_task_status(request, task_id, *args, **kwargs)
