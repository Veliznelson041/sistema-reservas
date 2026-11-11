from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from psycopg2.extras import DateTimeTZRange

from .emails import enviar_cancelacion, enviar_confirmacion
from .forms import RegistroForm, ReservaForm
from .models import Pago, Recurso, Reserva
from .permisos import complejos_del_usuario
from .servicios import cancelar_con_devolucion, registrar_pago
from .utils import pesos

METODOS_ONLINE = [
    (valor, nombre) for valor, nombre in Pago.Metodo.choices
    if valor != Pago.Metodo.EFECTIVO
]

COLORES_ADMIN = {
    'finalizada': ('#dc2626', '#ffffff'),   # rojo
    'confirmada': ('#16a34a', '#ffffff'),   # verde: confirmada y con seña paga
    'pendiente': ('#facc15', '#713f12'),    # amarillo: sin pagar
    'cancelada': ('#f97316', '#ffffff'),    # naranja
}
COLOR_PROPIA = '#4f46e5'    # violeta
COLOR_AJENA = '#94a3b8'     # gris


def registro(request):
    form = RegistroForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        usuario = form.save()
        login(request, usuario)
        return redirect('mis_reservas')
    return render(request, 'registration/registro.html', {'form': form})


def _recursos_reservables():
    return (
        Recurso.objects.filter(activo=True, complejo__activo=True)
        .select_related('complejo')
        .order_by('complejo__nombre', 'nombre')
    )


@login_required
def crear_reserva(request):
    recursos = _recursos_reservables()
    if request.method == 'POST':
        form = ReservaForm(request.POST, recursos=recursos)
    else:
        form = ReservaForm(initial={
            'inicio': request.GET.get('inicio'),
            'recurso': request.GET.get('recurso') or None,
        }, recursos=recursos)

    if request.method == 'POST' and form.is_valid():
        datos = form.cleaned_data
        recurso = datos['recurso']
        Reserva.objects.liberar_vencidas()
        # Monto y seña SIEMPRE se calculan en el servidor, nunca vienen del navegador
        monto = recurso.calcular_monto(datos['inicio'], datos['fin'])
        sena = recurso.calcular_sena(monto)
        gratis = monto == 0
        try:
            with transaction.atomic():
                reserva = Reserva.objects.create(
                    usuario=request.user,
                    recurso=recurso,
                    periodo=DateTimeTZRange(datos['inicio'], datos['fin']),
                    monto=monto,
                    sena=sena,
                    estado=(Reserva.Estado.CONFIRMADA if gratis
                            else Reserva.Estado.PENDIENTE),
                    vence_en=(None if gratis else timezone.now() + timedelta(
                        minutes=recurso.complejo.minutos_pago)),
                )
        except IntegrityError:
            form.add_error(None, 'Ese horario ya fue reservado. Elegí otro.')
        else:
            if gratis:
                enviar_confirmacion(reserva)
                messages.success(request, '¡Reserva confirmada!')
                return redirect('mis_reservas')
            return redirect('pagar_reserva', pk=reserva.pk)

    return render(request, 'reservas/crear.html', {
        'form': form,
        'recursos_js': {
            str(r.pk): {
                'precio': float(r.precio_hora),
                'sena': r.sena_porcentaje,
                'complejo': r.complejo.nombre,
                'apertura': r.complejo.hora_apertura,
                'cierre': r.complejo.hora_cierre,
                'duracion_max': r.complejo.duracion_max_horas,
                'anticipacion': r.complejo.anticipacion_cancelacion_horas,
                'minutos_pago': r.complejo.minutos_pago,
            }
            for r in recursos
        },
    })


@login_required
def pagar_reserva(request, pk):
    Reserva.objects.liberar_vencidas()
    reserva = get_object_or_404(Reserva, pk=pk, usuario=request.user)

    if reserva.estado == Reserva.Estado.CONFIRMADA:
        return redirect('mis_reservas')
    if reserva.estado != Reserva.Estado.PENDIENTE:
        messages.error(
            request,
            'Se agotó el tiempo para pagar y el horario fue liberado. Volvé a reservar.',
        )
        return redirect('crear_reserva')

    if request.method == 'POST':
        metodo = request.POST.get('metodo')
        if metodo not in dict(METODOS_ONLINE):
            messages.error(request, 'Elegí un método de pago.')
        else:
            pago = registrar_pago(reserva, metodo)
            if pago:
                enviar_confirmacion(pago.reserva)
                messages.success(request, '¡Seña recibida! Tu reserva está confirmada.')
                return redirect('mis_reservas')
            messages.error(request, 'No se pudo procesar el pago: la reserva venció.')
            return redirect('crear_reserva')

    segundos = int((reserva.vence_en - timezone.now()).total_seconds())
    return render(request, 'reservas/pagar.html', {
        'reserva': reserva,
        'metodos': METODOS_ONLINE,
        'segundos': max(segundos, 0),
    })


@login_required
def mis_reservas(request):
    reservas = (
        Reserva.objects.filter(usuario=request.user, cliente_nombre='')
        .select_related('recurso__complejo')
        .prefetch_related('pagos')
        .order_by('-creada_en')
    )
    return render(request, 'reservas/mis_reservas.html', {'reservas': reservas})


@login_required
@require_POST
def cancelar_reserva(request, pk):
    reserva = get_object_or_404(Reserva, pk=pk, usuario=request.user)
    if not reserva.puede_cancelarse():
        horas = reserva.recurso.complejo.anticipacion_cancelacion_horas
        messages.error(
            request, f'Solo se puede cancelar con {horas} horas de anticipación.'
        )
        return redirect('mis_reservas')
    devuelto = cancelar_con_devolucion(reserva)
    enviar_cancelacion(reserva)
    texto = 'Reserva cancelada.'
    if devuelto:
        texto += f' Se registró la devolución de {pesos(devuelto)}.'
    messages.info(request, texto)
    return redirect('mis_reservas')


def _metodo_de_pago(reserva):
    pagos = sorted(
        (p for p in reserva.pagos.all() if p.estado == Pago.Estado.APROBADO),
        key=lambda p: p.pk,
    )
    if not pagos:
        return 'Sin pagar' if reserva.estado == 'pendiente' else 'Sin pago registrado'
    texto = ' + '.join(
        f'{p.get_metodo_display()} ({p.get_concepto_display().lower()})' for p in pagos
    )
    return f'{texto} · devuelto' if reserva.estado == 'cancelada' else texto


def _filas_de_pago(r):
    filas = [['Total', pesos(r.monto)], ['Pagado', pesos(r.total_pagado)]]
    if r.estado == 'confirmada':
        filas.append(['Saldo a cobrar en el local', pesos(r.saldo)])
    filas.append(['Método de pago', _metodo_de_pago(r)])
    return filas


@login_required
def eventos_calendario(request):
    # Las pendientes vencidas se liberan: dejan de ocupar el horario
    Reserva.objects.liberar_vencidas()
    # Complejos que este usuario administra (vacío si es un cliente común)
    ids_admin = set(complejos_del_usuario(request.user).values_list('pk', flat=True))

    visibles = Q(estado__in=['pendiente', 'confirmada'])
    if ids_admin:   # las canceladas las ve solo quien administra ese complejo
        visibles |= Q(estado='cancelada', recurso__complejo_id__in=ids_admin)

    reservas = (
        Reserva.objects.filter(visibles)
        .select_related('recurso__complejo', 'usuario')
        .prefetch_related('pagos')
    )
    recurso_id = request.GET.get('recurso')
    if recurso_id and recurso_id.isdigit():
        reservas = reservas.filter(recurso_id=int(recurso_id))

    def fecha(dt):
        return timezone.localtime(dt).strftime('%d/%m/%Y %H:%M')

    eventos = []
    for r in reservas:
        complejo = r.recurso.complejo
        # El rol se decide POR COMPLEJO: ser admin de uno no da acceso a los demás
        es_admin = complejo.pk in ids_admin
        mia = r.usuario_id == request.user.id and not r.cliente_nombre
        etiqueta = f'{complejo.nombre} · {r.recurso.nombre}'
        detalle = [
            ['Complejo', complejo.nombre],
            ['Cancha', r.recurso.nombre],
            ['Desde', fecha(r.periodo.lower)],
            ['Hasta', fecha(r.periodo.upper)],
        ]
        if es_admin:
            color, texto = COLORES_ADMIN[r.estado_clave]
            titulo = f'{etiqueta} · {r.cliente} ({r.estado_texto})'
            cliente = r.cliente + (' (mostrador)' if r.cliente_nombre else '')
            detalle = [['Cliente', cliente]] + detalle + [
                ['Estado', r.estado_texto]
            ] + _filas_de_pago(r)
        elif mia:
            color, texto = COLOR_PROPIA, '#ffffff'
            titulo = f'{etiqueta} (mía)'
            detalle += [['Estado', r.estado_texto]] + _filas_de_pago(r)
        else:
            color, texto = COLOR_AJENA, '#ffffff'
            titulo = f'{etiqueta} · ocupada'
            detalle += [['Estado', 'Ocupada']]

        eventos.append({
            'id': r.pk,
            'title': titulo,
            'start': r.periodo.lower.isoformat(),
            'end': r.periodo.upper.isoformat(),
            'color': color,
            'textColor': texto,
            'extendedProps': {'detalle': detalle, 'titulo': etiqueta},
        })
    return JsonResponse(eventos, safe=False)


@login_required
def calendario(request):
    return render(request, 'reservas/calendario.html', {
        'recursos': _recursos_reservables(),
    })