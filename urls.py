from django.conf.urls import patterns, url

urlpatterns = patterns('',
    # for download request from resource landing page
    url(r'^download/(?P<path>.*)$', 'django_irods.views.download'),
    # for download request from REST API
    url(r'^download/(?P<path>.*)/(?P<rest_call>[a-z]+)$', 'django_irods.views.download',
        name='file_download'),
    # for AJAX poll from resource landing page
    url(r'^check_task_status/$', 'django_irods.views.check_task_status'),
    # for REST API poll
    url(r'^check_task_status/(?P<task_id>[A-z0-9]+)$', 'django_irods.views.check_task_status',
        name='check_task_status'),
)