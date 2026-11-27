from django.urls import path

from cloud_optimizer import actions, api, dashboard, ec2_pages, lifecycle, s3_pages, views

urlpatterns = [
    path('', views.home, name='home'),
    path('status/', dashboard.deployment_status, name='deployment_status'),
    path('findings/', dashboard.findings_page, name='findings'),
    path('ec2/', ec2_pages.page, name='ec2_inventory'),
    path('ec2/<str:instance_id>/', ec2_pages.page, name='ec2_detail'),
    path('s3/', s3_pages.page, name='s3_inventory'),
    path('s3/<str:bucket_name>/', s3_pages.page, name='s3_detail'),
    path('preview/', dashboard.preview, name='dashboard_preview'),
    path('health/', views.health),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('auth/start/', views.login_start, name='login_start'),
    path('auth/callback/', views.login_callback, name='login_callback'),
    path('api/status/', api.status),
    path('api/scans/', api.scans),
    path('api/resources/', api.resources),
    path('api/resources/<str:resource_type>/<str:resource_id>/', api.resource),
    path('api/findings/', api.findings),
    path('api/costs/', api.costs),
    path('api/alerts/', api.alerts),
    path('api/actions/', api.actions),
    path('api/actions/<str:request_id>/', actions.action_status),
    path('api/ec2/<str:instance_id>/<str:action>/', actions.request_action),
    path('api/s3/<str:bucket>/lifecycle/', lifecycle.lifecycle),
]
