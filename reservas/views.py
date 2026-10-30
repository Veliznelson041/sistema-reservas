from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from psycopg2.extras import DateTimeTZRange

from .forms import ReservaForm
from django.http import JsonResponse
from .models import Recurso, Reserva


def registro(request):
    form = UserCreationForm(request.POST or None)
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
        try:
            with transaction.atomic():
                Reserva.objects.create(
                    usuario=request.user,
                    recurso=datos['recurso'],
                    periodo=DateTimeTZRange(datos['inicio'], datos['fin']),
                )
        except IntegrityError:
            form.add_error(None, 'Ese horario ya fue reservado. Elegí otro.')
        else:
            messages.success(request, '¡Reserva confirmada!')
            return redirect('mis_reservas')
    return render(request, 'reservas/crear.html', {'form': form})


@login_required
def mis_reservas(request):
    reservas = (
        Reserva.objects.filter(usuario=request.user)
        .select_related('recurso')
        .order_by('-creada_en')
    )
    return render(request, 'reservas/mis_reservas.html', {'reservas': reservas})


@login_required
@require_POST
def cancelar_reserva(request, pk):
    reserva = get_object_or_404(Reserva, pk=pk, usuario=request.user)
    reserva.estado = Reserva.Estado.CANCELADA
    reserva.save()
    messages.info(request, 'Reserva cancelada.')
    return redirect('mis_reservas')


@login_required
def eventos_calendario(request):
    reservas = Reserva.objects.filter(estado='confirmada').select_related('recurso')
    recurso_id = request.GET.get('recurso')
    if recurso_id:
        reservas = reservas.filter(recurso_id=recurso_id)

    eventos = [
        {
            'title': f'{r.recurso.nombre}' + (' (mía)' if r.usuario_id == request.user.id else ''),
            'start': r.periodo.lower.isoformat(),
            'end': r.periodo.upper.isoformat(),
            'color': '#1b7f3b' if r.usuario_id == request.user.id else '#6c757d',
        }
        for r in reservas
    ]
    return JsonResponse(eventos, safe=False)


@login_required
def calendario(request):
    recursos = Recurso.objects.filter(activo=True)
    return render(request, 'reservas/calendario.html', {'recursos': recursos})
