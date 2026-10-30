from django.urls import path
from . import views

urlpatterns = [
    path('', views.mis_reservas, name='mis_reservas'),
    path('reservar/', views.crear_reserva, name='crear_reserva'),
    path('cancelar/<int:pk>/', views.cancelar_reserva, name='cancelar_reserva'),
    path('registro/', views.registro, name='registro'),
    path('calendario/', views.calendario, name='calendario'),
    path('calendario/eventos/', views.eventos_calendario, name='eventos_calendario'),
]