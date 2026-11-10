from django.urls import path

from cloud_optimizer import views

urlpatterns = [
    path('', views.home, name='home'),
    path('health/', views.health),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('setup/', views.setup, name='setup'),
    path('users/', views.users_view, name='users'),
    path('users/<str:user_id>/', views.user_update, name='user_update'),
]
