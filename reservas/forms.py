from django import forms
from django.utils import timezone

from .models import Recurso

from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import get_user_model

from datetime import timedelta
from django.conf import settings


class ReservaForm(forms.Form):
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
        if inicio < timezone.now():
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