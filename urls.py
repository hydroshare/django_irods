from django.conf.urls import patterns, url

urlpatterns = patterns(
    '',
    url(r'^download/(?P<irods_backend_slug>\w*?)/(?P<path>.*)/$',
        'django_irods.views.download'),
)
