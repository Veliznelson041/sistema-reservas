from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import wraps
from io import BytesIO
from xml.sax.saxutils import escape

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from psycopg2.extras import DateTimeTZRange
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

from .emails import enviar_cancelacion
from .forms import MostradorForm, MovimientoForm
from .models import Movimiento, Pago, Recurso, Reserva
from .permisos import complejos_del_usuario
from .servicios import cancelar_con_devolucion, cobrar_saldo, registrar_pago
from .utils import pesos

CERO = Decimal('0')
POR_PAGINA = 5
MESES_CORTOS = ['ene', 'feb', 'mar', 'abr', 'may', 'jun',
                'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
MESES_LARGOS = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
                'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']
ESTADOS_FILTRO = [
    ('pendiente', 'Pendiente de pago'),
    ('confirmada', 'Confirmada'),
    ('finalizada', 'Finalizada'),
    ('saldo', 'Con saldo por cobrar'),
    ('cancelada', 'Cancelada'),
    ('expirada', 'Vencida'),
]


def staff_required(view):
    """Solo staff asignado a algún complejo (o superusuario).

    Deja en el request:
      request.complejos        -> complejos que el usuario puede gestionar (permisos)
      request.complejo_activo  -> el elegido en el selector (o el único que tiene)
      request.alcance          -> complejos que se muestran en pantalla
    """
    @login_required
    @wraps(view)
    def envoltura(request, *args, **kwargs):
        complejos = complejos_del_usuario(request.user)
        if not request.user.is_staff or not (
            request.user.is_superuser or complejos.exists()
        ):
            raise PermissionDenied
        activo = None
        activo_id = request.session.get('complejo_activo')
        if activo_id:
            activo = complejos.filter(pk=activo_id).first()
        if activo is None and complejos.count() == 1:
            activo = complejos.first()
        request.complejos = complejos
        request.complejo_activo = activo
        request.alcance = complejos.filter(pk=activo.pk) if activo else complejos
        return view(request, *args, **kwargs)
    return envoltura


# ---------- utilidades ----------

def qs_sin(request, *claves):
    """Parámetros de la URL actual sin las claves dadas (para los links de paginación)."""
    params = request.GET.copy()
    for clave in claves:
        params.pop(clave, None)
    return params.urlencode()


def volver_a(request, por_defecto):
    """Redirige a la página de origen (filtros y página incluidos) si es una URL propia."""
    siguiente = request.POST.get('next', '')
    if url_has_allowed_host_and_scheme(siguiente, allowed_hosts={request.get_host()}):
        return redirect(siguiente)
    return redirect(por_defecto)


def parsear_mes(texto):
    """Convierte 'AAAA-MM' en el primer día de ese mes (por defecto, el actual)."""
    try:
        anio, mes = map(int, texto.split('-'))
        return date(anio, mes, 1)
    except (ValueError, AttributeError):
        return timezone.localdate().replace(day=1)


def siguiente_mes(d):
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def movimientos_del_mes(mes, alcance):
    return Movimiento.objects.filter(
        complejo__in=alcance, fecha__gte=mes, fecha__lt=siguiente_mes(mes)
    )


def totales(qs):
    agg = qs.aggregate(
        ingresos=Sum('monto', filter=Q(tipo='ingreso')),
        gastos=Sum('monto', filter=Q(tipo='gasto')),
    )
    ingresos = agg['ingresos'] or CERO
    gastos = agg['gastos'] or CERO
    return {'ingresos': ingresos, 'gastos': gastos, 'balance': ingresos - gastos}


def rango_del_dia(dia):
    inicio = timezone.make_aware(datetime.combine(dia, time.min))
    return DateTimeTZRange(inicio, inicio + timedelta(days=1))


# ---------- selector de complejo ----------

@staff_required
@require_POST
def elegir_complejo(request):
    elegido = request.POST.get('complejo', '')
    if elegido.isdigit() and request.complejos.filter(pk=int(elegido)).exists():
        request.session['complejo_activo'] = int(elegido)
    else:
        request.session.pop('complejo_activo', None)   # "Todos"
    return volver_a(request, reverse('panel'))


# ---------- resumen ----------

@staff_required
def dashboard(request):
    Reserva.objects.liberar_vencidas()
    alcance = request.alcance
    hoy = timezone.localdate()
    mes = hoy.replace(day=1)
    reservas_base = Reserva.objects.filter(recurso__complejo__in=alcance)

    reservas_hoy = (
        reservas_base.filter(estado='confirmada', periodo__overlap=rango_del_dia(hoy))
        .select_related('recurso__complejo', 'usuario')
        .order_by('periodo')
    )

    # Últimos 6 meses para el gráfico
    meses, anio, nro = [], mes.year, mes.month
    for _ in range(6):
        meses.append(date(anio, nro, 1))
        nro -= 1
        if nro == 0:
            anio, nro = anio - 1, 12
    grafico = {'etiquetas': [], 'ingresos': [], 'gastos': []}
    for d in reversed(meses):
        t = totales(movimientos_del_mes(d, alcance))
        grafico['etiquetas'].append(f'{MESES_CORTOS[d.month - 1]} {d.year}')
        grafico['ingresos'].append(float(t['ingresos']))
        grafico['gastos'].append(float(t['gastos']))

    varios = alcance.count() > 1
    por_cancha = [
        {
            'nombre': (f"{f['reserva__recurso__complejo__nombre']} · " if varios else '')
                      + f['reserva__recurso__nombre'],
            'total': f['total'],
        }
        for f in (
            movimientos_del_mes(mes, alcance)
            .filter(tipo='ingreso', reserva__isnull=False)
            .values('reserva__recurso__id', 'reserva__recurso__nombre',
                    'reserva__recurso__complejo__nombre')
            .annotate(total=Sum('monto'))
            .order_by('-total')
        )
    ]

    confirmado = (
        reservas_base.filter(estado='confirmada').aggregate(t=Sum('monto'))['t'] or CERO
    )
    cobrado = (
        Pago.objects.filter(
            estado='aprobado', reserva__estado='confirmada',
            reserva__recurso__complejo__in=alcance,
        ).aggregate(t=Sum('monto'))['t'] or CERO
    )

    return render(request, 'panel/dashboard.html', {
        'seccion': 'resumen',
        'hoy': hoy,
        'kpis': totales(movimientos_del_mes(mes, alcance)),
        'reservas_hoy': reservas_hoy,
        'pendientes': reservas_base.filter(estado='pendiente').count(),
        'grafico': grafico,
        'por_cancha': por_cancha,
        'saldo_por_cobrar': max(confirmado - cobrado, CERO),
    })


# ---------- reservas ----------

@staff_required
def reservas(request):
    Reserva.objects.liberar_vencidas()
    qs = (
        Reserva.objects.filter(recurso__complejo__in=request.alcance)
        .select_related('recurso__complejo', 'usuario')
        .prefetch_related('pagos')
        .annotate(cobrado=Coalesce(
            Sum('pagos__monto', filter=Q(pagos__estado='aprobado')),
            Value(CERO),
            output_field=DecimalField(max_digits=10, decimal_places=2),
        ))
        .order_by('-periodo', '-id')
    )

    estado = request.GET.get('estado', '')
    recurso = request.GET.get('recurso', '')
    fecha = request.GET.get('fecha', '')
    ahora = timezone.now()
    if estado == 'finalizada':
        qs = qs.filter(estado='confirmada', periodo__endswith__lte=ahora)
    elif estado == 'confirmada':
        qs = qs.filter(estado='confirmada', periodo__endswith__gt=ahora)
    elif estado == 'saldo':
        qs = qs.filter(estado='confirmada', monto__gt=F('cobrado'))
    elif estado:
        qs = qs.filter(estado=estado)
    if recurso.isdigit():
        qs = qs.filter(recurso_id=int(recurso))
    if fecha:
        try:
            qs = qs.filter(periodo__overlap=rango_del_dia(date.fromisoformat(fecha)))
        except ValueError:
            pass

    pagina = Paginator(qs, POR_PAGINA).get_page(request.GET.get('pagina'))

    return render(request, 'panel/reservas.html', {
        'seccion': 'reservas',
        'reservas': pagina,
        'query': qs_sin(request, 'pagina'),
        'recursos': (Recurso.objects.filter(complejo__in=request.alcance)
                     .select_related('complejo').order_by('complejo__nombre', 'nombre')),
        'estados': ESTADOS_FILTRO,
        'metodos': Pago.Metodo.choices,
        'f_estado': estado, 'f_recurso': recurso, 'f_fecha': fecha,
    })


@staff_required
@require_POST
def cancelar(request, pk):
    # El permiso se verifica contra TODOS los complejos del usuario, no solo el visible
    reserva = get_object_or_404(Reserva, pk=pk, recurso__complejo__in=request.complejos)
    if reserva.finalizada:
        messages.error(
            request, 'Esa reserva ya finalizó: no se puede cancelar ni devolver.'
        )
    elif not reserva.admin_puede_cancelar:
        messages.error(request, 'Esa reserva ya no se puede cancelar.')
    else:
        devuelto = cancelar_con_devolucion(reserva)
        enviar_cancelacion(reserva)
        texto = f'Reserva #{reserva.pk} cancelada.'
        if devuelto:
            texto += f' Se registró la devolución de {pesos(devuelto)}.'
        messages.info(request, texto)
    return volver_a(request, reverse('panel_reservas'))


@staff_required
@require_POST
def cobrar(request, pk):
    reserva = get_object_or_404(Reserva, pk=pk, recurso__complejo__in=request.complejos)
    metodo = request.POST.get('metodo')
    if metodo not in Pago.Metodo.values:
        messages.error(request, 'Elegí un método de pago.')
    else:
        pago = cobrar_saldo(reserva, metodo)
        if pago:
            messages.success(
                request,
                f'Cobrado {pesos(pago.monto)} de la reserva #{reserva.pk} '
                f'({pago.get_metodo_display()}).',
            )
        else:
            messages.error(request, 'Esa reserva no tiene saldo para cobrar.')
    return volver_a(request, reverse('panel_reservas'))


@staff_required
def mostrador(request):
    recursos = Recurso.objects.filter(activo=True, complejo__in=request.alcance)
    form = MostradorForm(request.POST or None, recursos=recursos)
    if request.method == 'POST' and form.is_valid():
        datos = form.cleaned_data
        Reserva.objects.liberar_vencidas()
        recurso = datos['recurso']
        monto = recurso.calcular_monto(datos['inicio'], datos['fin'])
        cobra_ahora = datos['cobro'] in ('sena', 'total') and monto > 0
        try:
            with transaction.atomic():
                reserva = Reserva.objects.create(
                    usuario=request.user,
                    cliente_nombre=datos['cliente'],
                    recurso=recurso,
                    periodo=DateTimeTZRange(datos['inicio'], datos['fin']),
                    monto=monto,
                    sena=recurso.calcular_sena(monto),
                    estado=(Reserva.Estado.PENDIENTE if cobra_ahora
                            else Reserva.Estado.CONFIRMADA),
                    vence_en=(timezone.now() + timedelta(minutes=5)
                              if cobra_ahora else None),
                )
        except IntegrityError:
            form.add_error(None, 'Ese horario ya está ocupado.')
        else:
            if cobra_ahora:
                registrar_pago(reserva, datos['metodo'])
                if datos['cobro'] == 'total':
                    cobrar_saldo(reserva, datos['metodo'])
            reserva = Reserva.objects.get(pk=reserva.pk)
            messages.success(
                request,
                f'Reserva de {datos["cliente"]} registrada. '
                f'Cobrado: {pesos(reserva.total_pagado)}. '
                f'Saldo por cobrar: {pesos(reserva.saldo)}.',
            )
            return redirect('panel_reservas')

    return render(request, 'panel/mostrador.html', {'seccion': 'mostrador', 'form': form})


# ---------- finanzas ----------

@staff_required
def finanzas(request):
    mes = parsear_mes(request.GET.get('mes'))

    if request.method == 'POST':
        form = MovimientoForm(request.POST, complejos=request.alcance)
        if form.is_valid():
            form.save()
            messages.success(request, 'Movimiento registrado.')
            fecha = form.cleaned_data['fecha']
            return redirect(f"{reverse('panel_finanzas')}?mes={fecha:%Y-%m}")
    else:
        form = MovimientoForm(complejos=request.alcance)

    qs = movimientos_del_mes(mes, request.alcance).select_related('reserva')
    tipo = request.GET.get('tipo', '')
    movimientos = qs.filter(tipo=tipo) if tipo in ('ingreso', 'gasto') else qs

    nombres = dict(Movimiento.Categoria.choices)
    filas_desglose = [
        {'tipo': f['tipo'], 'nombre': nombres[f['categoria']], 'total': f['total']}
        for f in (
            qs.values('tipo', 'categoria')
            .annotate(total=Sum('monto'))
            .order_by('-tipo', '-total')   # primero ingresos, después gastos
        )
    ]

    return render(request, 'panel/finanzas.html', {
        'seccion': 'finanzas',
        'mes': mes,
        'mes_anterior': (mes - timedelta(days=1)).replace(day=1).strftime('%Y-%m'),
        'mes_siguiente': siguiente_mes(mes).strftime('%Y-%m'),
        'kpis': totales(qs),
        'tipo': tipo,
        'form': form,
        'movimientos': Paginator(movimientos, POR_PAGINA).get_page(request.GET.get('pagina')),
        'desglose': Paginator(filas_desglose, POR_PAGINA).get_page(
            request.GET.get('pagina_desglose')
        ),
        'qs_mov': qs_sin(request, 'pagina'),
        'qs_des': qs_sin(request, 'pagina_desglose'),
    })


@staff_required
@require_POST
def eliminar_movimiento(request, pk):
    # Solo los movimientos cargados a mano; los de reservas son el registro contable
    get_object_or_404(
        Movimiento, pk=pk, reserva__isnull=True, complejo__in=request.complejos
    ).delete()
    messages.info(request, 'Movimiento eliminado.')
    mes = parsear_mes(request.POST.get('mes'))
    return volver_a(request, f"{reverse('panel_finanzas')}?mes={mes:%Y-%m}")


@staff_required
def exportar_pdf(request):
    mes = parsear_mes(request.GET.get('mes'))
    qs = movimientos_del_mes(mes, request.alcance)
    kpis = totales(qs)
    nombres_complejos = ', '.join(c.nombre for c in request.alcance) or 'Sin complejos'

    azul = colors.HexColor('#312e81')
    gris = colors.HexColor('#64748b')
    verde = colors.HexColor('#16a34a')
    rojo = colors.HexColor('#dc2626')
    claro = colors.HexColor('#eef2ff')

    base = getSampleStyleSheet()
    titulo = ParagraphStyle('titulo', parent=base['Title'], alignment=0,
                            textColor=azul, fontSize=22, spaceAfter=2)
    sub = ParagraphStyle('sub', parent=base['Normal'], textColor=gris, spaceAfter=10)
    h2 = ParagraphStyle('h2', parent=base['Heading2'], textColor=azul,
                        spaceBefore=16, spaceAfter=6)
    celda = ParagraphStyle('celda', parent=base['Normal'], fontSize=9, leading=11)

    ahora = timezone.localtime()
    elementos = [
        Paragraph('Informe financiero', titulo),
        Paragraph(
            f'{escape(nombres_complejos)} - '
            f'{MESES_LARGOS[mes.month - 1].capitalize()} {mes.year} - '
            f'Generado el {ahora:%d/%m/%Y %H:%M}', sub),
    ]

    # Resumen
    resumen = Table(
        [['Ingresos', 'Gastos', 'Balance'],
         [pesos(kpis['ingresos']), pesos(kpis['gastos']), pesos(kpis['balance'])]],
        colWidths=[5.8 * cm] * 3,
    )
    resumen.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), claro),
        ('TEXTCOLOR', (0, 0), (-1, 0), gris),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('FONTNAME', (0, 1), (-1, 1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 1), (-1, 1), 15),
        ('TEXTCOLOR', (0, 1), (0, 1), verde),
        ('TEXTCOLOR', (1, 1), (1, 1), rojo),
        ('TEXTCOLOR', (2, 1), (2, 1), verde if kpis['balance'] >= 0 else rojo),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elementos.append(resumen)

    estilo_tabla = [
        ('BACKGROUND', (0, 0), (-1, 0), azul),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
        ('LINEBELOW', (0, 0), (-1, -1), 0.25, colors.HexColor('#e2e8f0')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]

    # Desglose por categoría
    nombres = dict(Movimiento.Categoria.choices)
    desglose = [['Tipo', 'Categoría', 'Total']]
    for f in qs.values('tipo', 'categoria').annotate(total=Sum('monto')).order_by('-tipo', '-total'):
        desglose.append([
            'Ingreso' if f['tipo'] == 'ingreso' else 'Gasto',
            nombres[f['categoria']], pesos(f['total']),
        ])
    elementos.append(Paragraph('Desglose por categoría', h2))
    if len(desglose) > 1:
        t = Table(desglose, colWidths=[3 * cm, 10 * cm, 4.4 * cm], repeatRows=1)
        t.setStyle(TableStyle(estilo_tabla + [('ALIGN', (2, 0), (2, -1), 'RIGHT')]))
        elementos.append(t)
    else:
        elementos.append(Paragraph('Sin movimientos en este período.', base['Normal']))

    # Detalle
    detalle = [['Fecha', 'Tipo', 'Categoría', 'Concepto', 'Monto']]
    for m in qs.order_by('fecha', 'id'):
        signo = '+' if m.tipo == 'ingreso' else '-'
        detalle.append([
            m.fecha.strftime('%d/%m/%Y'),
            m.get_tipo_display(),
            Paragraph(escape(m.get_categoria_display()), celda),
            Paragraph(escape(m.concepto), celda),
            f'{signo}{pesos(m.monto)}',
        ])
    if len(detalle) > 1:
        elementos.append(Paragraph('Detalle de movimientos', h2))
        t = Table(detalle, colWidths=[2.2 * cm, 1.8 * cm, 3.8 * cm, 6.6 * cm, 3 * cm],
                  repeatRows=1)
        t.setStyle(TableStyle(estilo_tabla + [('ALIGN', (4, 0), (4, -1), 'RIGHT')]))
        elementos.append(t)

    def pie(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(gris)
        canvas.drawString(1.8 * cm, 1 * cm, 'TurnosPro')
        canvas.drawRightString(A4[0] - 1.8 * cm, 1 * cm, f'Página {doc.page}')
        canvas.restoreState()

    buffer = BytesIO()
    SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm,
        topMargin=1.8 * cm, bottomMargin=1.8 * cm, title=f'Finanzas {mes:%m-%Y}',
    ).build(elementos, onFirstPage=pie, onLaterPages=pie)

    respuesta = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    respuesta['Content-Disposition'] = f'attachment; filename="finanzas-{mes:%Y-%m}.pdf"'
    return respuesta