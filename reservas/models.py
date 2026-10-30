from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Recurso(models.Model):
    nombre = models.CharField(max_length=100)
    descripcion = models.TextField(blank=True)
    precio_hora = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    activo = models.BooleanField(default=True)

    def calcular_monto(self, inicio, fin):
        horas = Decimal((fin - inicio).total_seconds()) / Decimal(3600)
        return (self.precio_hora * horas).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )

    def __str__(self):
        return self.nombre


class ReservaManager(models.Manager):
    def liberar_vencidas(self):
        """Libera los horarios de reservas pendientes que no se pagaron a tiempo."""
        return self.filter(
            estado='pendiente', vence_en__lt=timezone.now()
        ).update(estado='expirada')


class Reserva(models.Model):
    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente de pago'
        CONFIRMADA = 'confirmada', 'Confirmada'
        CANCELADA = 'cancelada', 'Cancelada'
        EXPIRADA = 'expirada', 'Vencida'

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reservas'
    )
    recurso = models.ForeignKey(
        Recurso, on_delete=models.CASCADE, related_name='reservas'
    )
    periodo = DateTimeRangeField()
    estado = models.CharField(
        max_length=20, choices=Estado.choices, default=Estado.CONFIRMADA
    )
    monto = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    vence_en = models.DateTimeField(null=True, blank=True)
    creada_en = models.DateTimeField(auto_now_add=True)

    objects = ReservaManager()

    class Meta:
        constraints = [
            ExclusionConstraint(
                name='evitar_reservas_solapadas',
                expressions=[
                    ('periodo', RangeOperators.OVERLAPS),
                    ('recurso', RangeOperators.EQUAL),
                ],
                # Las pendientes también bloquean el horario mientras se paga
                condition=Q(estado__in=['pendiente', 'confirmada']),
            ),
        ]

    def puede_cancelarse(self):
        limite = timezone.now() + timedelta(
            hours=settings.RESERVA_CANCELAR_ANTICIPACION_HORAS
        )
        return self.estado == self.Estado.CONFIRMADA and self.periodo.lower > limite

    def __str__(self):
        return f'{self.recurso} - {self.periodo.lower:%d/%m/%Y %H:%M}'


class Pago(models.Model):
    class Metodo(models.TextChoices):
        TARJETA = 'tarjeta', 'Tarjeta de crédito/débito'
        TRANSFERENCIA = 'transferencia', 'Transferencia'
        BILLETERA = 'billetera', 'Billetera virtual'

    class Estado(models.TextChoices):
        APROBADO = 'aprobado', 'Aprobado'
        RECHAZADO = 'rechazado', 'Rechazado'

    reserva = models.ForeignKey(Reserva, on_delete=models.CASCADE, related_name='pagos')
    monto = models.DecimalField(max_digits=10, decimal_places=2)
    metodo = models.CharField(max_length=20, choices=Metodo.choices)
    estado = models.CharField(
        max_length=20, choices=Estado.choices, default=Estado.APROBADO
    )
    referencia = models.CharField(max_length=64, blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Pago #{self.pk} - {self.monto}'


class Movimiento(models.Model):
    class Tipo(models.TextChoices):
        INGRESO = 'ingreso', 'Ingreso'
        GASTO = 'gasto', 'Gasto'

    class Categoria(models.TextChoices):
        RESERVA = 'reserva', 'Reservas'
        DEVOLUCION = 'devolucion', 'Devoluciones'
        MANTENIMIENTO = 'mantenimiento', 'Mantenimiento'
        SERVICIOS = 'servicios', 'Servicios (luz, agua, internet)'
        SUELDOS = 'sueldos', 'Sueldos'
        INSUMOS = 'insumos', 'Insumos'
        OTROS = 'otros', 'Otros'

    tipo = models.CharField(max_length=10, choices=Tipo.choices)
    categoria = models.CharField(max_length=20, choices=Categoria.choices)
    concepto = models.CharField(max_length=200)
    monto = models.DecimalField(max_digits=10, decimal_places=2)
    fecha = models.DateField(default=timezone.localdate)
    reserva = models.ForeignKey(
        Reserva, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='movimientos',
    )
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-fecha', '-id']

    def __str__(self):
        return f'{self.get_tipo_display()}: {self.concepto} (${self.monto})'