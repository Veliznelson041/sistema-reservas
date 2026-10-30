from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from psycopg2.extras import DateTimeTZRange

from .forms import ReservaForm
from .models import Reserva


def registro(request):
    form = UserCreationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        usuario = form.save()
        login(request, usuario)
        return redirect('mis_reservas')
    return render(request, 'registration/registro.html', {'form': form})


@login_required
def crear_reserva(request):
    form = ReservaForm(request.POST or None)
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