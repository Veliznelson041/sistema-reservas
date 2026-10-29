from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import Q


class Recurso(models.Model):
    nombre = models.CharField(max_length=100)
    descripcion = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    def __str__(self):
        return self.nombre


class Reserva(models.Model):
    class Estado(models.TextChoices):
        CONFIRMADA = 'confirmada', 'Confirmada'
        CANCELADA = 'cancelada', 'Cancelada'

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
    creada_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            ExclusionConstraint(
                name='evitar_reservas_solapadas',
                expressions=[
                    ('periodo', RangeOperators.OVERLAPS),
                    ('recurso', RangeOperators.EQUAL),
                ],
                condition=Q(estado='confirmada'),
            ),
        ]

    def __str__(self):
        return f'{self.recurso} - {self.periodo.lower:%d/%m/%Y %H:%M}'