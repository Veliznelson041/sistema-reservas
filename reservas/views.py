from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from psycopg2.extras import DateTimeTZRange

from .emails import enviar_cancelacion, enviar_confirmacion
from .forms import RegistroForm, ReservaForm
from .models import Pago, Recurso, Reserva
from .servicios import cancelar_con_devolucion, registrar_pago


def registro(request):
    form = RegistroForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        usuario = form.save()
        login(request, usuario)
        return redirect('mis_reservas')
    return render(request, 'registration/registro.html', {'form': form})


@login_required
def crear_reserva(request):
    if request.method == 'POST':
        form = ReservaForm(request.POST)
    else:
        form = ReservaForm(initial={
            'inicio': request.GET.get('inicio'),
            'recurso': request.GET.get('recurso') or None,
        })

    if request.method == 'POST' and form.is_valid():
        datos = form.cleaned_data
        recurso = datos['recurso']
        Reserva.objects.liberar_vencidas()
        # El monto SIEMPRE se calcula en el servidor, nunca viene del navegador
        monto = recurso.calcular_monto(datos['inicio'], datos['fin'])
        gratis = monto == 0
        try:
            with transaction.atomic():
                reserva = Reserva.objects.create(
                    usuario=request.user,
                    recurso=recurso,
                    periodo=DateTimeTZRange(datos['inicio'], datos['fin']),
                    monto=monto,
                    estado=(Reserva.Estado.CONFIRMADA if gratis
                            else Reserva.Estado.PENDIENTE),
                    vence_en=(None if gratis else timezone.now() + timedelta(
                        minutes=settings.RESERVA_PAGO_MINUTOS)),
                )
        except IntegrityError:
            form.add_error(None, 'Ese horario ya fue reservado. Elegí otro.')
        else:
            if gratis:
                enviar_confirmacion(reserva)
                messages.success(request, '¡Reserva confirmada!')
                return redirect('mis_reservas')
            return redirect('pagar_reserva', pk=reserva.pk)

    recursos = Recurso.objects.filter(activo=True)
    return render(request, 'reservas/crear.html', {
        'form': form,
        'precios': {str(r.pk): float(r.precio_hora) for r in recursos},
        'anticipacion': settings.RESERVA_CANCELAR_ANTICIPACION_HORAS,
        'duracion_max': settings.RESERVA_DURACION_MAX_HORAS,
        'apertura': settings.RESERVA_HORA_APERTURA,
        'minutos_pago': settings.RESERVA_PAGO_MINUTOS,
    })


@login_required
def pagar_reserva(request, pk):
    Reserva.objects.liberar_vencidas()
    reserva = get_object_or_404(Reserva, pk=pk, usuario=request.user)

    if reserva.estado == Reserva.Estado.CONFIRMADA:
        return redirect('mis_reservas')
    if reserva.estado != Reserva.Estado.PENDIENTE:
        messages.error(request, 'Se agotó el tiempo para pagar y el horario fue liberado. Volvé a reservar.')
        return redirect('crear_reserva')

    if request.method == 'POST':
        metodo = request.POST.get('metodo')
        if metodo not in Pago.Metodo.values:
            messages.error(request, 'Elegí un método de pago.')
        else:
            pago = registrar_pago(reserva, metodo)
            if pago:
                enviar_confirmacion(pago.reserva)
                messages.success(request, '¡Pago aprobado! Tu reserva está confirmada.')
                return redirect('mis_reservas')
            messages.error(request, 'No se pudo procesar el pago: la reserva venció.')
            return redirect('crear_reserva')

    segundos = int((reserva.vence_en - timezone.now()).total_seconds())
    return render(request, 'reservas/pagar.html', {
        'reserva': reserva,
        'metodos': Pago.Metodo.choices,
        'segundos': max(segundos, 0),
    })


@login_required
@require_POST
def cancelar_reserva(request, pk):
    reserva = get_object_or_404(Reserva, pk=pk, usuario=request.user)
    if not reserva.puede_cancelarse():
        messages.error(request, 'Solo se puede cancelar con 24 horas de anticipación.')
        return redirect('mis_reservas')
    devuelto = cancelar_con_devolucion(reserva)
    enviar_cancelacion(reserva)
    texto = 'Reserva cancelada.'
    if devuelto:
        texto += f' Se registró la devolución de ${devuelto}.'
    messages.info(request, texto)
    return redirect('mis_reservas')


@login_required
def mis_reservas(request):
    reservas = (
        Reserva.objects.filter(usuario=request.user)
        .select_related('recurso')
        .order_by('-creada_en')
    )
    return render(request, 'reservas/mis_reservas.html', {'reservas': reservas})


@login_required
def eventos_calendario(request):
    reservas = Reserva.objects.filter(
        estado__in=['pendiente', 'confirmada']
    ).select_related('recurso')

    recurso_id = request.GET.get('recurso')
    if recurso_id:
        reservas = reservas.filter(recurso_id=recurso_id)

    eventos = [
        {
            'title': f'{r.recurso.nombre}' + (
                ' (mía)' if r.usuario_id == request.user.id else ''
            ),
            'start': r.periodo.lower.isoformat(),
            'end': r.periodo.upper.isoformat(),
            'color': (
                '#f59e0b' if r.estado == 'pendiente'
                else '#4f46e5' if r.usuario_id == request.user.id
                else '#94a3b8'
            ),
        }
        for r in reservas
    ]

    return JsonResponse(eventos, safe=False)


@login_required
def calendario(request):
    recursos = Recurso.objects.filter(activo=True)
    return render(request, 'reservas/calendario.html', {'recursos': recursos})
