from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Movimiento, Pago, Reserva


@transaction.atomic
def registrar_pago(reserva, metodo):
    """Confirma la reserva y registra el ingreso. Devuelve el Pago o None si no corresponde."""
    reserva = Reserva.objects.select_for_update().get(pk=reserva.pk)
    if reserva.estado != Reserva.Estado.PENDIENTE:
        return None
    if reserva.vence_en and reserva.vence_en < timezone.now():
        return None

    pago = Pago.objects.create(reserva=reserva, monto=reserva.monto, metodo=metodo)
    reserva.estado = Reserva.Estado.CONFIRMADA
    reserva.vence_en = None
    reserva.save(update_fields=['estado', 'vence_en'])
    Movimiento.objects.create(
        tipo=Movimiento.Tipo.INGRESO,
        categoria=Movimiento.Categoria.RESERVA,
        concepto=f'Reserva #{reserva.pk} - {reserva.recurso}',
        monto=reserva.monto,
        reserva=reserva,
    )
    return pago


@transaction.atomic
def cancelar_con_devolucion(reserva):
    """Cancela la reserva. Si estaba pagada, registra la devolución como gasto."""
    reserva.estado = Reserva.Estado.CANCELADA
    reserva.save(update_fields=['estado'])
    pagada = reserva.pagos.filter(estado=Pago.Estado.APROBADO).exists()
    if pagada and reserva.monto > 0:
        Movimiento.objects.create(
            tipo=Movimiento.Tipo.GASTO,
            categoria=Movimiento.Categoria.DEVOLUCION,
            concepto=f'Devolución reserva #{reserva.pk}',
            monto=reserva.monto,
            reserva=reserva,
        )
        return reserva.monto
    return Decimal('0')