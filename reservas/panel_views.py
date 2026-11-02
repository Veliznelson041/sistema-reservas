
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from psycopg2.extras import DateTimeTZRange

from .emails import enviar_cancelacion
from .forms import MostradorForm, MovimientoForm
from .models import Movimiento, Recurso, Reserva
from .servicios import cancelar_con_devolucion, registrar_pago

from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


CERO = Decimal('0')
MESES_CORTOS = ['ene', 'feb', 'mar', 'abr', 'may', 'jun',
                'jul', 'ago', 'sep', 'oct', 'nov', 'dic']

MESES_LARGOS = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
                'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']
ESTADOS_FILTRO = [
    ('pendiente', 'Pendiente de pago'),
    ('confirmada', 'Confirmada'),
    ('finalizada', 'Finalizada'),
    ('cancelada', 'Cancelada'),
    ('expirada', 'Vencida'),
]

def staff_required(view):
    @login_required
    @wraps(view)
    def envoltura(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return envoltura


# ---------- utilidades ----------

def parsear_mes(texto):
    """Convierte 'AAAA-MM' en el primer día de ese mes (por defecto, el actual)."""
    try:
        anio, mes = map(int, texto.split('-'))
        return date(anio, mes, 1)
    except (ValueError, AttributeError):
        return timezone.localdate().replace(day=1)


def siguiente_mes(d):
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def movimientos_del_mes(mes):
    return Movimiento.objects.filter(fecha__gte=mes, fecha__lt=siguiente_mes(mes))


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


# ---------- resumen ----------

@staff_required
def dashboard(request):
    Reserva.objects.liberar_vencidas()
    hoy = timezone.localdate()
    mes = hoy.replace(day=1)

    reservas_hoy = (
        Reserva.objects.filter(estado='confirmada', periodo__overlap=rango_del_dia(hoy))
        .select_related('recurso', 'usuario')
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
        t = totales(movimientos_del_mes(d))
        grafico['etiquetas'].append(f'{MESES_CORTOS[d.month - 1]} {d.year}')
        grafico['ingresos'].append(float(t['ingresos']))
        grafico['gastos'].append(float(t['gastos']))

    por_cancha = [
        {'nombre': f['reserva__recurso__nombre'], 'total': f['total']}
        for f in (
            movimientos_del_mes(mes)
            .filter(tipo='ingreso', reserva__isnull=False)
            .values('reserva__recurso__nombre')
            .annotate(total=Sum('monto'))
            .order_by('-total')
        )
    ]

    return render(request, 'panel/dashboard.html', {
        'seccion': 'resumen',
        'hoy': hoy,
        'kpis': totales(movimientos_del_mes(mes)),
        'reservas_hoy': reservas_hoy,
        'pendientes': Reserva.objects.filter(estado='pendiente').count(),
        'grafico': grafico,
        'por_cancha': por_cancha,
    })


# ---------- reservas ----------

@staff_required
def reservas(request):
    Reserva.objects.liberar_vencidas()
    qs = Reserva.objects.select_related('recurso', 'usuario').order_by('-periodo')

    estado = request.GET.get('estado', '')
    recurso = request.GET.get('recurso', '')
    fecha = request.GET.get('fecha', '')
    ahora = timezone.now()
    if estado == 'finalizada':
        qs = qs.filter(estado='confirmada', periodo__endswith__lte=ahora)
    elif estado == 'confirmada':
        qs = qs.filter(estado='confirmada', periodo__endswith__gt=ahora)
    elif estado:
        qs = qs.filter(estado=estado)
    if recurso.isdigit():
        qs = qs.filter(recurso_id=int(recurso))
    if fecha:
        try:
            qs = qs.filter(periodo__overlap=rango_del_dia(date.fromisoformat(fecha)))
        except ValueError:
            pass

    return render(request, 'panel/reservas.html', {
        'seccion': 'reservas',
        'reservas': qs[:200],
        'recursos': Recurso.objects.all(),
        'estados': ESTADOS_FILTRO,
        'f_estado': estado, 'f_recurso': recurso, 'f_fecha': fecha,
    })


@staff_required
@require_POST
def cancelar(request, pk):
    reserva = get_object_or_404(Reserva, pk=pk)
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
            texto += f' Se registró la devolución de ${devuelto}.'
        messages.info(request, texto)
    return redirect('panel_reservas')


@staff_required
def mostrador(request):
    form = MostradorForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        datos = form.cleaned_data
        Reserva.objects.liberar_vencidas()
        recurso = datos['recurso']
        monto = recurso.calcular_monto(datos['inicio'], datos['fin'])
        gratis = monto == 0
        try:
            with transaction.atomic():
                reserva = Reserva.objects.create(
                    usuario=request.user,
                    cliente_nombre=datos['cliente'],
                    recurso=recurso,
                    periodo=DateTimeTZRange(datos['inicio'], datos['fin']),
                    monto=monto,
                    estado=(Reserva.Estado.CONFIRMADA if gratis
                            else Reserva.Estado.PENDIENTE),
                    vence_en=None if gratis else timezone.now() + timedelta(minutes=5),
                )
        except IntegrityError:
            form.add_error(None, 'Ese horario ya está ocupado.')
        else:
            if not gratis:
                registrar_pago(reserva, datos['metodo'])
            messages.success(
                request, f'Reserva de {datos["cliente"]} registrada (${monto}).'
            )
            return redirect('panel_reservas')

    return render(request, 'panel/mostrador.html', {'seccion': 'mostrador', 'form': form})


# ---------- finanzas ----------

@staff_required
def finanzas(request):
    mes = parsear_mes(request.GET.get('mes'))

    if request.method == 'POST':
        form = MovimientoForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Movimiento registrado.')
            fecha = form.cleaned_data['fecha']
            return redirect(f"{reverse('panel_finanzas')}?mes={fecha:%Y-%m}")
    else:
        form = MovimientoForm()

    qs = movimientos_del_mes(mes).select_related('reserva')
    tipo = request.GET.get('tipo', '')
    movimientos = qs.filter(tipo=tipo) if tipo in ('ingreso', 'gasto') else qs

    nombres = dict(Movimiento.Categoria.choices)
    desglose = (
        qs.values('tipo', 'categoria').annotate(total=Sum('monto')).order_by('-total')
    )
    ingresos_cat = [{'nombre': nombres[f['categoria']], 'total': f['total']}
                    for f in desglose if f['tipo'] == 'ingreso']
    gastos_cat = [{'nombre': nombres[f['categoria']], 'total': f['total']}
                  for f in desglose if f['tipo'] == 'gasto']

    return render(request, 'panel/finanzas.html', {
        'seccion': 'finanzas',
        'mes': mes,
        'mes_anterior': (mes - timedelta(days=1)).replace(day=1).strftime('%Y-%m'),
        'mes_siguiente': siguiente_mes(mes).strftime('%Y-%m'),
        'kpis': totales(qs),
        'movimientos': movimientos,
        'tipo': tipo,
        'form': form,
        'ingresos_cat': ingresos_cat,
        'gastos_cat': gastos_cat,
    })


@staff_required
@require_POST
def eliminar_movimiento(request, pk):
    # Solo los movimientos cargados a mano; los de reservas son el registro contable
    get_object_or_404(Movimiento, pk=pk, reserva__isnull=True).delete()
    mes = parsear_mes(request.POST.get('mes'))
    messages.info(request, 'Movimiento eliminado.')
    return redirect(f"{reverse('panel_finanzas')}?mes={mes:%Y-%m}")


def pesos(valor):
    texto = f'{valor:,.2f}'
    return '$' + texto.replace(',', '_').replace('.', ',').replace('_', '.')


@staff_required
def exportar_pdf(request):
    mes = parsear_mes(request.GET.get('mes'))
    qs = movimientos_del_mes(mes)
    kpis = totales(qs)

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
        canvas.drawString(1.8 * cm, 1 * cm, 'Sistema de reservas')
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