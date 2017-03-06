import mimetypes

from django.http import StreamingHttpResponse

from django_irods.storage import IrodsStorage


def chunker(stream, chunk_s):
    while 1:
        yield stream.read(chunk_s)


def download(request, irods_backend_slug, path, *args, **kwargs):
    # ignore trailing /s & add initial slash
    filename = [x for x in path.split('/') if x][-1]
    path = '/{0}'.format(path)

    storage = IrodsStorage(irods_backend=irods_backend_slug)
    stream = storage.open(path, "r")
    response = StreamingHttpResponse(chunker(stream, storage.chunk_size))

    response['Content-Disposition'] = "attachment; filename=%s" % filename
    response['Content-Type'] = mimetypes.guess_type(filename)
    response['Content-Length'] = storage.size(path)
    return response
