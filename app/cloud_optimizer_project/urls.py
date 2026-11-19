from django.urls import path

from cloud_optimizer import actions, api, dashboard, ec2_pages, views

urlpatterns = [
    path('', views.home, name='home'),
    path('status/', dashboard.deployment_status, name='deployment_status'),
    path('ec2/', ec2_pages.page, name='ec2_inventory'),
    path('ec2/<str:instance_id>/', ec2_pages.page, name='ec2_detail'),
    path('preview/', dashboard.preview, name='dashboard_preview'),
    path('health/', views.health),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('setup/', views.setup, name='setup'),
    path('users/', views.users_view, name='users'),
    path('users/<str:user_id>/', views.user_update, name='user_update'),
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
]
