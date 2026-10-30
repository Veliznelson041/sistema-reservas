from django import forms
from django.utils import timezone

from .models import Recurso


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
        if inicio and fin:
            if fin <= inicio:
                raise forms.ValidationError('El fin debe ser posterior al inicio.')
            if inicio < timezone.now():
                raise forms.ValidationError('No se puede reservar en el pasado.')
        return datos