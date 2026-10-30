from django.contrib import admin

from .models import Movimiento, Pago, Recurso, Reserva


@admin.register(Recurso)
class RecursoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'precio_hora', 'activo')
    list_editable = ('precio_hora', 'activo')


@admin.register(Reserva)
class ReservaAdmin(admin.ModelAdmin):
    list_display = ('id', 'recurso', 'usuario', 'periodo', 'monto', 'estado')
    list_filter = ('estado', 'recurso')


@admin.register(Pago)
class PagoAdmin(admin.ModelAdmin):
    list_display = ('id', 'reserva', 'monto', 'metodo', 'estado', 'creado_en')
    list_filter = ('metodo', 'estado')


@admin.register(Movimiento)
class MovimientoAdmin(admin.ModelAdmin):
    list_display = ('fecha', 'tipo', 'categoria', 'concepto', 'monto')
    list_filter = ('tipo', 'categoria')