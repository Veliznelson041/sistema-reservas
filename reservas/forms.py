from django import forms
from django.utils import timezone

from .models import Movimiento, Pago, Recurso

from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import get_user_model

from datetime import timedelta
from django.conf import settings


class ReservaForm(forms.Form):
    tolerancia_minutos = 0
    recurso = forms.ModelChoiceField(queryset=Recurso.objects.filter(activo=True))
    inicio = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        input_formats=['%Y-%m-%dT%H:%M'],
    )
    fin = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        input_formats=['%Y-%m-%dT%H:%M'],
    )

    def clean(self):
        datos = super().clean()
        inicio, fin = datos.get('inicio'), datos.get('fin')
        if not (inicio and fin):
            return datos

        if fin <= inicio:
            raise forms.ValidationError('El fin debe ser posterior al inicio.')
        if inicio < timezone.now() - timedelta(minutes=self.tolerancia_minutos):
            raise forms.ValidationError('No se puede reservar en el pasado.')

        maxima = timedelta(hours=settings.RESERVA_DURACION_MAX_HORAS)
        if fin - inicio > maxima:
            raise forms.ValidationError(
                f'La reserva no puede durar más de '
                f'{settings.RESERVA_DURACION_MAX_HORAS} horas.'
            )

        inicio_local = timezone.localtime(inicio)
        fin_local = timezone.localtime(fin)
        if (inicio_local.hour < settings.RESERVA_HORA_APERTURA
                or inicio_local.date() != fin_local.date()):
            raise forms.ValidationError(
                f'Las reservas son desde las {settings.RESERVA_HORA_APERTURA}:00 '
                f'y deben terminar el mismo día.'
            )
        return datos


class RegistroForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ('username', 'email')


class MostradorForm(ReservaForm):
    tolerancia_minutos = 60  # permite cargar a quien llegó recién al local
    field_order = ['cliente', 'recurso', 'inicio', 'fin', 'cobro', 'metodo']

    cliente = forms.CharField(max_length=100, label='Nombre del cliente')
    cobro = forms.ChoiceField(
        choices=[
            ('sena', 'Cobrar la seña'),
            ('total', 'Cobrar el total ahora'),
            ('nada', 'No cobrar todavía (se cobra al finalizar)'),
        ],
        initial='sena', label='Cobro ahora',
    )
    metodo = forms.ChoiceField(
        choices=Pago.Metodo.choices, initial=Pago.Metodo.EFECTIVO,
        label='Método de pago',
    )


class MovimientoForm(forms.ModelForm):
    class Meta:
        model = Movimiento
        fields = ['tipo', 'categoria', 'concepto', 'monto', 'fecha']
        labels = {
            'tipo': 'Tipo', 'categoria': 'Categoría',
            'concepto': 'Concepto', 'monto': 'Monto ($)', 'fecha': 'Fecha',
        }
        widgets = {'fecha': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Reservas y devoluciones se registran solas
        self.fields['categoria'].choices = [
            c for c in Movimiento.Categoria.choices
            if c[0] not in ('reserva', 'devolucion', 'sena', 'saldo')
        ]

    def clean_monto(self):
        monto = self.cleaned_data['monto']
        if monto <= 0:
            raise forms.ValidationError('El monto debe ser mayor a cero.')
        return monto

