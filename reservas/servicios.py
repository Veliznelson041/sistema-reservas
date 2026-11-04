from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import Movimiento, Pago, Reserva


@transaction.atomic
def registrar_pago(reserva, metodo):
    """Cobra la seña online, confirma la reserva y registra el ingreso."""
    reserva = Reserva.objects.select_for_update().get(pk=reserva.pk)
    if reserva.estado != Reserva.Estado.PENDIENTE:
        return None
    if reserva.vence_en and reserva.vence_en < timezone.now():
        return None

    monto = reserva.a_pagar_online
    pago = Pago.objects.create(
        reserva=reserva, monto=monto, metodo=metodo, concepto=Pago.Concepto.SENA
    )
    reserva.estado = Reserva.Estado.CONFIRMADA
    reserva.vence_en = None
    reserva.save(update_fields=['estado', 'vence_en'])
    Movimiento.objects.create(
        tipo=Movimiento.Tipo.INGRESO,
        categoria=Movimiento.Categoria.SENA,
        concepto=f'Seña reserva #{reserva.pk} - {reserva.recurso}',
        monto=monto,
        reserva=reserva,
    )
    return pago


@transaction.atomic
def cobrar_saldo(reserva, metodo):
    """Cobra en el local lo que falta de una reserva confirmada (antes o después del turno)."""
    reserva = Reserva.objects.select_for_update().get(pk=reserva.pk)
    if reserva.estado != Reserva.Estado.CONFIRMADA:
        return None
    saldo = reserva.saldo
    if saldo <= 0:
        return None

    pago = Pago.objects.create(
        reserva=reserva, monto=saldo, metodo=metodo, concepto=Pago.Concepto.SALDO
    )
    Movimiento.objects.create(
        tipo=Movimiento.Tipo.INGRESO,
        categoria=Movimiento.Categoria.SALDO,
        concepto=f'Saldo reserva #{reserva.pk} - {reserva.recurso}',
        monto=saldo,
        reserva=reserva,
    )
    return pago


@transaction.atomic
def cancelar_con_devolucion(reserva):
    """Cancela la reserva y devuelve lo pagado (la seña) registrándolo como gasto."""
    if reserva.finalizada:
        raise ValueError('La reserva ya finalizó: no se puede cancelar ni devolver.')
    reserva.estado = Reserva.Estado.CANCELADA
    reserva.save(update_fields=['estado'])
    pagado = (
        reserva.pagos.filter(estado=Pago.Estado.APROBADO)
        .aggregate(total=Sum('monto'))['total']
        or Decimal('0')
    )
    if pagado > 0:
        Movimiento.objects.create(
            tipo=Movimiento.Tipo.GASTO,
            categoria=Movimiento.Categoria.DEVOLUCION,
            concepto=f'Devolución reserva #{reserva.pk}',
            monto=pagado,
            reserva=reserva,
        )
    return pagado