from django.conf.urls import patterns, url

urlpatterns = patterns('',

    # users API

    url(r'^download/(?P<path>.*)$', 'django_irods.views.download'),
    url(r'^poll_for_download/$', 'django_irods.views.poll_for_download'),
)