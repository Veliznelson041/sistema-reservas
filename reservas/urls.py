from django.urls import path
from . import panel_views, views

urlpatterns = [
    path('', views.mis_reservas, name='mis_reservas'),
    path('reservar/', views.crear_reserva, name='crear_reserva'),
    path('cancelar/<int:pk>/', views.cancelar_reserva, name='cancelar_reserva'),
    path('registro/', views.registro, name='registro'),
    path('calendario/', views.calendario, name='calendario'),
    path('calendario/eventos/', views.eventos_calendario, name='eventos_calendario'),
    path('pagar/<int:pk>/', views.pagar_reserva, name='pagar_reserva'),
    path('panel/', panel_views.dashboard, name='panel'),
    path('panel/reservas/', panel_views.reservas, name='panel_reservas'),
    path('panel/reservas/<int:pk>/cancelar/', panel_views.cancelar, name='panel_cancelar'),
    path('panel/mostrador/', panel_views.mostrador, name='panel_mostrador'),
    path('panel/finanzas/', panel_views.finanzas, name='panel_finanzas'),
    path('panel/finanzas/pdf/', panel_views.exportar_pdf, name='panel_exportar'),
    path('panel/finanzas/<int:pk>/eliminar/', panel_views.eliminar_movimiento, name='panel_eliminar_movimiento'),
    path('panel/reservas/<int:pk>/cobrar/', panel_views.cobrar, name='panel_cobrar'),
        path('panel/complejo/', panel_views.elegir_complejo, name='panel_complejo'),

]