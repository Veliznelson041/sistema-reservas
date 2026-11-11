from django.contrib import admin

from .models import Complejo, Movimiento, Pago, Recurso, Reserva


@admin.register(Complejo)
class ComplejoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'slug', 'activo', 'canchas')
    list_editable = ('activo',)
    prepopulated_fields = {'slug': ('nombre',)}
    filter_horizontal = ('administradores',)
    fieldsets = (
        (None, {'fields': ('nombre', 'slug', 'descripcion', 'url_web', 'activo')}),
        ('Administradores', {
            'fields': ('administradores',),
            'description': 'Solo entran al panel los usuarios con "Es staff" tildado.',
        }),
        ('Reglas de reserva', {'fields': (
            'hora_apertura', 'hora_cierre', 'duracion_max_horas',
            'anticipacion_cancelacion_horas', 'minutos_pago',
        )}),
    )

    @admin.display(description='Canchas')
    def canchas(self, obj):
        return obj.recursos.count()


@admin.register(Recurso)
class RecursoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'complejo', 'precio_hora', 'sena_porcentaje', 'activo')
    list_editable = ('precio_hora', 'sena_porcentaje', 'activo')
    list_filter = ('complejo',)


@admin.register(Reserva)
class ReservaAdmin(admin.ModelAdmin):
    list_display = ('id', 'recurso', 'usuario', 'periodo', 'monto', 'estado')
    list_filter = ('estado', 'recurso__complejo', 'recurso')


@admin.register(Pago)
class PagoAdmin(admin.ModelAdmin):
    list_display = ('id', 'reserva', 'monto', 'concepto', 'metodo', 'estado', 'creado_en')
    list_filter = ('concepto', 'metodo', 'estado')


@admin.register(Movimiento)
class MovimientoAdmin(admin.ModelAdmin):
    list_display = ('fecha', 'complejo', 'tipo', 'categoria', 'concepto', 'monto')
    list_filter = ('complejo', 'tipo', 'categoria')