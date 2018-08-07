import datetime
import json
import mimetypes
import os
import random
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, FileResponse, HttpResponseRedirect
from rest_framework.decorators import api_view

from django_irods import icommands
from django_irods.storage import IrodsStorage
from hs_core.hydroshare import check_resource_type
from hs_core.hydroshare.hs_bagit import create_bag_files
from hs_core.hydroshare.resource import FILE_SIZE_LIMIT
from hs_core.signals import pre_download_file, pre_check_bag_flag
from hs_core.tasks import create_bag_by_irods, create_temp_zip, delete_zip
from hs_core.views.utils import authorize, ACTION_TO_AUTHORIZE
from . import models as m
from .icommands import Session, GLOBAL_SESSION
from hs_core.models import ResourceFile

import logging
logger = logging.getLogger(__name__)


def download(request, path, rest_call=False, use_async=True, use_reverse_proxy=True,
             *args, **kwargs):
    """ perform a download request, either asynchronously or synchronously

    :param request: the request object.
    :param path: the path of the thing to be downloaded.
    :param rest_call: True if calling from REST API
    :param use_async: True means to utilize asynchronous creation of objects to download.
    :param use_reverse_proxy: True means to utilize NGINX reverse proxy for streaming.

    The following variables are computed:

    * `path` is the public path of the thing to be downloaded.
    * `irods_path` is the location of "path" in irods.
    * `output_path` is the output path to be reported in the response object.
    * `irods_output_path` is the location of "output_path" in irods

    and there are six cases:

    1. path points to a single file that is not a single-file-aggregation
       object in a composite resource.
    2. path points to a single file that is a single-file-aggregation
       object in a composite resource.
    3. path points to a metadata object that may need updating.
    4. path points to a bag that needs to be updated and then returned.
    5. path points to a folder that needs to be zipped.
    6. path points to a previously zipped file that was zipped asynchronously.

    In cases 1, 3, 4, and 6 the output path is the input path.
    In cases 2 and 5, the output path is created as a result of the operation
    of zipping the object and its metadata into a new zipfile.
    """
    logger.debug("request path is {}".format(path))

    split_path_strs = path.split('/')

    # initialize case variables 
    is_bag_download = False
    is_zip_download = False
    is_zip_request = False
    is_sf_agg_file = False

    if split_path_strs[0] == 'bags':
        is_bag_download = True
        # format is bags/{rid}.zip
        logger.debug("fetching rid from bags field 1 = {}".format(split_path_strs[1]))
        res_id = os.path.splitext(split_path_strs[1])[0]
    elif split_path_strs[0] == 'zips':
        is_zip_download = True
        # zips prefix now means that we are following up on an asynchronous download request
        # format is zips/{date}/{zip-uuid}/{path}.zip where {path} contains the rid
        logger.debug("fetching rid from zips field 3 = {}".format(split_path_strs[3]))
        res_id = split_path_strs[3]
    else:
        logger.debug("fetching rid from public path field 0 = {}".format(split_path_strs[0]))
        res_id = split_path_strs[0]

    logger.debug("resource id is {}".format(res_id))

    # now we have the resource Id and can authorize the request
    # if the resource does not exist in django, authorized will be false
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

    # default values are changed later
    if res.is_federated:
        irods_path = os.path.join(res.resource_federation_path, path)
    else:
        irods_path = path
    output_path = path
    irods_output_path = irods_path

    # folder requests are automatically zipped
    if not is_bag_download and not is_zip_download:  # path points into resource: should I zip it?
        logger.debug("split_path_strs[1:] is {}".format(split_path_strs[1:]))
        store_path = u'/'.join(split_path_strs[1:])  # data/contents/{path-to-something}
        logger.debug("store path is {}".format(store_path))
        if res.is_folder(store_path):
            is_zip_request = True
            daily_date = datetime.datetime.today().strftime('%Y-%m-%d')
            output_path = "zips/{}/{}/{}.zip".format(daily_date, uuid4.hex(), path)
            if res.is_federated: 
                output_irods_path = os.path.join(res.resource_federation_path, output_path)
            else: 
                output_irods_path = output_path 

    # aggregation logic only applies if the download request isn't a bag, zipfile, or folder, 
    # and the thing to be downloaded is a "single file aggregation" object.
    if not is_bag_download and not is_zip_download and not is_zip_request and \
       res.resource_type == "CompositeResource":
        for f in ResourceFile.objects.filter(object_id=res.id):
            if path == f.storage_path:
                if f.has_logical_file and f.logical_file.is_single_file_aggregation:
                    is_sf_agg_file = True
                    daily_date = datetime.datetime.today().strftime('%Y-%m-%d')
                    output_path = "zips/{}/{}/{}.zip".format(daily_date, uuid4.hex(), path)
                    if res.is_federated: 
                        output_irods_path = os.path.join(res.resource_federation_path, output_path)
                    else: 
                        output_irods_path = output_path 

    # After this point, we have valid path, irods_path, output_path, and irods_output_path
    # The following flags have also been set:
    # * is_bag_download: download a bag in format bags/{rid}.zip
    # * is_zip_download: download a zipfile in format zips/{date}/{random guid}/{path}.zip
    # * is_zip_request: path is a folder; zip before returning
    # * is_sf_agg_file: path is a single-file aggregation in Composite Resource, return a zip
    # if none of these are set, it's a normal download

    # determine federation path
    istorage = res.get_irods_storage()
    if res.is_federated:
        # the resource is stored in federated zone
        session = icommands.ACTIVE_SESSION
    else:
        # TODO: From Alva: I do not understand the use case for changing the environment.
        # TODO: This seems an enormous potential vulnerability, as arguments are
        # TODO: passed from the URI directly to IRODS without verification.
        if 'environment' in kwargs:
            logger.debug("setting iRODS from environment")
            environment = int(kwargs['environment'])
            environment = m.RodsEnvironment.objects.get(pk=environment)
            session = Session("/tmp/django_irods", settings.IRODS_ICOMMANDS_PATH,
                              session_id=uuid4())
            session.create_environment(environment)
            session.run('iinit', None, environment.auth)
        elif getattr(settings, 'IRODS_GLOBAL_SESSION', False):
            logger.debug("using GLOBAL_SESSION")
            session = GLOBAL_SESSION
        elif icommands.ACTIVE_SESSION:
            logger.debug("using ACTIVE_SESSION")
            session = icommands.ACTIVE_SESSION
        else:
            raise KeyError('settings must have IRODS_GLOBAL_SESSION set '
                           'if there is no environment object')

    resource_cls = check_resource_type(res.resource_type)

    if is_bag_download:

    elif is_zip_download:
    elif is_zip_request or is_sf_agg_file:
        if res.resource_type == "CompositeResource":
            short_path = input_path[len('/data/contents/'):]  # strip /data/contents/
            res.create_aggregation_xml_documents(aggregation_name=short_path)

        if use_async:
            # TODO: Why is there a wait of 3 seconds here? Changes so far have been synchronous!
            # TODO: Ensure that this works for federated files.
            task = create_temp_zip.apply_async((res_id, irods_input_path, irods_output_path,
                                                is_sf_agg_file), countdown=3)
            # TODO: 20 minutes might not be enough if the zipfile is large.
            # TODO: this does not delete the working files! 
            delete_zip.apply_async((irods_output_path, ),
                                   countdown=(20 * 60))  # delete after 20 minutes

            if rest_call:
                return HttpResponse(json.dumps({'zip_status': 'Not ready',
                                                'task_id': task.task_id,
                                                'download_path': output_path}),
                                    content_type="application/json")
            request.session['task_id'] = task.task_id

            # TODO: this is mistaken for a bag download in the UI!
            # TODO: multiple asynchronous downloads don't stack!
            request.session['download_path'] = output_path  # path once async is done

            return HttpResponseRedirect(res.get_absolute_url())
        else:
            # TODO: ensure that this works in federated mode.
            ret_status = create_temp_zip(res_id, irods_input_path, irods_output_path, 
                                         is_sf_agg_file)
            delete_zip.apply_async((irods_output_path, ),
                                   countdown=(20 * 60))  # delete after 20 minutes
            if not ret_status:
                content_msg = "Zip cannot be created successfully. Check log for details."
                response = HttpResponse()
                if rest_call:
                    response.content = content_msg
                else:
                    response.content = "<h1>" + content_msg + "</h1>"
                return response

            logger.debug("output path is now {}".format(output_path))

    elif is_bag_download:

        bag_modified = istorage.getAVU(res.root_path, 'bag_modified')
        # make sure if bag_modified is not set to true, we still recreate the bag if the
        # bag file does not exist for some reason to resolve the error to download a nonexistent
        # bag when bag_modified is false due to the flag being out-of-sync with the real bag status
        if bag_modified is None or bag_modified.lower() == "false":
            # check whether the bag file exists
            bag_file_name = res_id + '.zip'
            output_path = os.path.join('bags', bag_file_name)
            if not res.is_federated:
                irods_output_path = output_path
            else:
                irods_output_path = os.path.join(res.resource_federation_path, output_path)
            # set bag_modified to 'true' if the bag does not exist so that it can be recreated
            # and the bag_modified AVU will be set correctly as well subsequently
            if not istorage.exists(irods_output_path):
                bag_modified = 'true'

        # send signal for pre_check_bag_flag
        # this generates metadata other than that generated by create_bag_files.
        pre_check_bag_flag.send(sender=resource_cls, resource=res)

        metadata_dirty = istorage.getAVU(res.root_path, 'metadata_dirty')
        if metadata_dirty is None or metadata_dirty.lower() == 'true':
            create_bag_files(res)
            bag_modified = "True"

        if bag_modified is None or bag_modified.lower() == "true":
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
                ret_status = create_bag_by_irods(res_id)
                if not ret_status:
                    content_msg = "Bag cannot be created successfully. Check log for details."
                    response = HttpResponse()
                    if rest_call:
                        response.content = content_msg
                    else:
                        response.content = "<h1>" + content_msg + "</h1>"
                    return response

    else:  # regular file download
        # if fetching main metadata files, then these need to be refreshed.
        if path.endswith("resourcemap.xml") or path.endswith('resourcemetadata.xml'):
            if metadata_dirty is None or metadata_dirty.lower() == 'true':
                create_bag_files(res)

        # send signal for pre download file
        # TODO: does not contain subdirectory information: duplicate refreshes possible
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
    # TODO: make sure this includes federation header.
    stdout = session.run("ils", None, "-l", path)[0].split()
    flen = int(stdout[3])

    # Allow reverse proxy if request was forwarded by nginx (HTTP_X_DJANGO_REVERSE_PROXY='true')
    # and reverse proxy is possible according to configuration (SENDFILE_ON=True)
    # and reverse proxy isn't overridden by user (use_reverse_proxy=True).

    if use_reverse_proxy and getattr(settings, 'SENDFILE_ON', False) and \
       'HTTP_X_DJANGO_REVERSE_PROXY' in request.META:

        # The NGINX sendfile abstraction is invoked as follows:
        # 1. The request to download a file enters this routine via the /rest_download or /download
        #    url in ./urls.py. It is redirected here from Django. The URI contains either the
        #    unqualified resource path or the federated resource path, depending upon whether
        #    the request is local or federated.
        # 2. This deals with unfederated resources by redirecting them to the uri
        #    /irods-data/{resource-id}/... on nginx. This URI is configured to read the file
        #    directly from the iRODS vault via NFS, and does not work for direct access to the
        #    vault due to the 'internal;' declaration in NGINX.
        # 3. This deals with federated resources by reading their path, matching local vaults, and
        #    redirecting to URIs that are in turn mapped to read from appropriate iRODS vaults. At
        #    present, the only one of these is /irods-user, which handles files whose federation
        #    path is stored in the variable 'userpath'.
        # 4. If there is no vault available for the resource, the file is transferred without
        #    NGINX, exactly as it was transferred previously.

        # If this path is resource_federation_path, then the file is a local user file
        userpath = '/' + os.path.join(
            getattr(settings, 'HS_USER_IRODS_ZONE', 'hydroshareuserZone'),
            'home',
            getattr(settings, 'HS_LOCAL_PROXY_USER_IN_FED_ZONE', 'localHydroProxy'))

        # stop NGINX targets that are non-existent from hanging forever.
        if not istorage.exists(path):
            content_msg = "file path {} does not exist in iRODS".format(path)
            response = HttpResponse(status=404)
            if rest_call:
                response.content = content_msg
            else:
                response.content = "<h1>" + content_msg + "</h1>"
            return response

        if not res.is_federated:
            # invoke X-Accel-Redirect on physical vault file in nginx
            response = HttpResponse(content_type=mtype)
            response['Content-Disposition'] = 'attachment; filename="{name}"'.format(
                name=path.split('/')[-1])
            response['Content-Length'] = flen
            response['X-Accel-Redirect'] = '/'.join([
                getattr(settings, 'IRODS_DATA_URI', '/irods-data'), path])
            logger.debug("Reverse proxying local {}".format(response['X-Accel-Redirect']))
            return response

        elif res.resource_federation_path == userpath:  # this guarantees a "user" resource
            # invoke X-Accel-Redirect on physical vault file in nginx
            # if path is full user path; strip federation prefix
            if path.startswith(userpath):
                path = path[len(userpath)+1:]
            # invoke X-Accel-Redirect on physical vault file in nginx
            response = HttpResponse(content_type=mtype)
            response['Content-Disposition'] = 'attachment; filename="{name}"'.format(
                name=path.split('/')[-1])
            response['Content-Length'] = flen
            response['X-Accel-Redirect'] = os.path.join(
                getattr(settings, 'IRODS_USER_URI', '/irods-user'), path)
            logger.debug("Reverse proxying user {}".format(response['X-Accel-Redirect']))
            return response

    # if we get here, none of the above conditions are true
    # if reverse proxy is enabled, then this is because the resource is remote and federated
    # OR the user specifically requested a non-proxied download.
    if flen <= FILE_SIZE_LIMIT:
        options = ('-',)  # we're redirecting to stdout.
        # this unusual way of calling works for federated or local resources
        logger.debug("Locally streaming {}".format(path))
        proc = session.run_safe('iget', None, path, *options)
        response = FileResponse(proc.stdout, content_type=mtype)
        response['Content-Disposition'] = 'attachment; filename="{name}"'.format(
            name=path.split('/')[-1])
        response['Content-Length'] = flen
        return response

    else:
        logger.debug("Rejecting download of > 1GB file {}".format(path))
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
