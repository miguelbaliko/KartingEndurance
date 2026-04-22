from django.urls import path
from . import views

urlpatterns = [
    path('', views.command_view, name='command'),
    path('strategist/', views.strategist_view, name='strategist'),
    path('api/state/', views.api_state, name='api_state'),
    path('api/manual/', views.api_manual, name='api_manual'),
    path('api/race/', views.api_race_control, name='api_race'),
]
