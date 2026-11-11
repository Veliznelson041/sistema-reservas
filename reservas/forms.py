from django import forms
from django.utils import timezone

from .models import Movimiento, Pago, Recurso

from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import get_user_model

from datetime import timedelta
from django.conf import settings


class ReservaForm(forms.Form):
    tolerancia_minutos = 0

    recurso = forms.ModelChoiceField(queryset=Recurso.objects.none())
    inicio = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        input_formats=['%Y-%m-%dT%H:%M'],
    )
    fin = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        input_formats=['%Y-%m-%dT%H:%M'],
    )

    def __init__(self, *args, recursos=None, **kwargs):
        super().__init__(*args, **kwargs)
        if recursos is None:
            recursos = Recurso.objects.filter(activo=True, complejo__activo=True)
        campo = self.fields['recurso']
        campo.queryset = recursos.select_related('complejo')
        campo.label_from_instance = lambda r: f'{r.complejo.nombre} · {r.nombre}'

    def clean(self):
        datos = super().clean()
        recurso, inicio, fin = datos.get('recurso'), datos.get('inicio'), datos.get('fin')
        if not (recurso and inicio and fin):
            return datos
        complejo = recurso.complejo

        if fin <= inicio:
            raise forms.ValidationError('El fin debe ser posterior al inicio.')
        if inicio < timezone.now() - timedelta(minutes=self.tolerancia_minutos):
            raise forms.ValidationError('No se puede reservar en el pasado.')
        if fin - inicio > timedelta(hours=complejo.duracion_max_horas):
            raise forms.ValidationError(
                f'En {complejo.nombre} la reserva no puede durar más de '
                f'{complejo.duracion_max_horas} horas.'
            )

        inicio_l, fin_l = timezone.localtime(inicio), timezone.localtime(fin)
        medianoche = (inicio_l + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if fin_l.date() == inicio_l.date():
            fin_horas = fin_l.hour + fin_l.minute / 60
        elif fin_l == medianoche:
            fin_horas = 24
        else:
            raise forms.ValidationError('La reserva debe terminar el mismo día.')
        inicio_horas = inicio_l.hour + inicio_l.minute / 60
        if inicio_horas < complejo.hora_apertura or fin_horas > complejo.hora_cierre:
            raise forms.ValidationError(
                f'{complejo.nombre} atiende de {complejo.hora_apertura}:00 '
                f'a {complejo.hora_cierre}:00.'
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
        fields = ['complejo', 'tipo', 'categoria', 'concepto', 'monto', 'fecha']
        labels = {
            'complejo': 'Complejo', 'tipo': 'Tipo', 'categoria': 'Categoría',
            'concepto': 'Concepto', 'monto': 'Monto ($)', 'fecha': 'Fecha',
        }
        widgets = {'fecha': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d')}

    def __init__(self, *args, complejos, **kwargs):
        super().__init__(*args, **kwargs)
        campo = self.fields['complejo']
        campo.queryset = complejos
        campo.empty_label = None
        if complejos.count() == 1:   # con un solo complejo no hace falta elegirlo
            campo.initial = complejos.first().pk
            campo.widget = forms.HiddenInput()
        # Reservas, señas, saldos y devoluciones se registran solos
        self.fields['categoria'].choices = [
            c for c in Movimiento.Categoria.choices
            if c[0] not in ('reserva', 'devolucion', 'sena', 'saldo')
        ]

    def clean_monto(self):
        monto = self.cleaned_data['monto']
        if monto <= 0:
            raise forms.ValidationError('El monto debe ser mayor a cero.')
        return monto

