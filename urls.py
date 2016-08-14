from django.conf.urls import patterns, url

urlpatterns = patterns('',

    # users API

    url(r'^download/(?P<path>.*)$', 'django_irods.views.download'),
    url(r'^download/(?P<path>.*)/(?P<rest_call>[a-z]+)$', 'django_irods.views.download'),
    url(r'^check_task_status/$', 'django_irods.views.check_task_status',
        name='check_task_status'),
)