import logging
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)


def _fecha(dt):
    return timezone.localtime(dt).strftime('%d/%m/%Y %H:%M')


def _enviar(asunto, mensaje, destinatario):
    try:
        send_mail(asunto, mensaje, None, [destinatario])
    except Exception:
        logger.exception('No se pudo enviar el email a %s', destinatario)


def enviar_confirmacion(reserva):
    if not reserva.usuario.email:
        return
    _enviar(
        'Reserva confirmada',
        f'Hola {reserva.usuario.username},\n\n'
        f'Tu reserva de {reserva.recurso} quedó confirmada.\n'
        f'Desde: {_fecha(reserva.periodo.lower)}\n'
        f'Hasta: {_fecha(reserva.periodo.upper)}\n',
        reserva.usuario.email,
    )


def enviar_cancelacion(reserva):
    if not reserva.usuario.email:
        return
    _enviar(
        'Reserva cancelada',
        f'Hola {reserva.usuario.username},\n\n'
        f'Se canceló tu reserva de {reserva.recurso} '
        f'del {_fecha(reserva.periodo.lower)}.\n',
        reserva.usuario.email,
    )