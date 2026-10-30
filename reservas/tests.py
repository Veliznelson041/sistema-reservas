import threading
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from psycopg2.extras import DateTimeTZRange

from .models import Recurso, Reserva

User = get_user_model()


def rango(inicio, horas=1):
    return DateTimeTZRange(inicio, inicio + timedelta(hours=horas))


class ReservaSolapamientoTests(TestCase):
    """Pruebas secuenciales de la restricción de exclusión."""

    def setUp(self):
        self.usuario = User.objects.create_user('nelson', password='1234')
        self.cancha1 = Recurso.objects.create(nombre='Cancha 1')
        self.cancha2 = Recurso.objects.create(nombre='Cancha 2')
        self.inicio = timezone.now() + timedelta(days=1)

    def crear(self, recurso, inicio, estado='confirmada'):
        return Reserva.objects.create(
            usuario=self.usuario, recurso=recurso,
            periodo=rango(inicio), estado=estado,
        )

    def test_rechaza_reserva_solapada(self):
        self.crear(self.cancha1, self.inicio)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.crear(self.cancha1, self.inicio + timedelta(minutes=30))

    def test_permite_reserva_contigua(self):
        self.crear(self.cancha1, self.inicio)
        self.crear(self.cancha1, self.inicio + timedelta(hours=1))
        self.assertEqual(Reserva.objects.count(), 2)

    def test_permite_mismo_horario_en_otro_recurso(self):
        self.crear(self.cancha1, self.inicio)
        self.crear(self.cancha2, self.inicio)
        self.assertEqual(Reserva.objects.count(), 2)

    def test_reserva_cancelada_libera_el_horario(self):
        self.crear(self.cancha1, self.inicio, estado='cancelada')
        self.crear(self.cancha1, self.inicio)
        self.assertEqual(Reserva.objects.count(), 2)


class ReservaConcurrenciaTests(TransactionTestCase):
    """Varios hilos intentan reservar el mismo horario a la vez."""

    def test_solo_una_reserva_gana_con_hilos_simultaneos(self):
        usuario = User.objects.create_user('nelson', password='1234')
        cancha = Recurso.objects.create(nombre='Cancha 1')
        inicio = timezone.now() + timedelta(days=1)

        cantidad_hilos = 10
        barrera = threading.Barrier(cantidad_hilos)
        resultados = []

        def intentar_reservar():
            try:
                barrera.wait()  # todos arrancan al mismo tiempo
                Reserva.objects.create(
                    usuario=usuario, recurso=cancha, periodo=rango(inicio),
                )
                resultados.append('ok')
            except IntegrityError:
                resultados.append('rechazada')
            finally:
                connection.close()  # cada hilo tiene su propia conexión

        hilos = [threading.Thread(target=intentar_reservar)
                 for _ in range(cantidad_hilos)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        self.assertEqual(resultados.count('ok'), 1)
        self.assertEqual(resultados.count('rechazada'), cantidad_hilos - 1)
        self.assertEqual(Reserva.objects.count(), 1)


from .forms import ReservaForm


class ReglasDeNegocioTests(TestCase):
    def setUp(self):
        self.cancha = Recurso.objects.create(nombre='Cancha 1')
        manana = timezone.localtime() + timedelta(days=2)
        self.inicio = manana.replace(hour=10, minute=0, second=0, microsecond=0)

    def datos(self, horas):
        fmt = '%Y-%m-%dT%H:%M'
        return {
            'recurso': self.cancha.pk,
            'inicio': self.inicio.strftime(fmt),
            'fin': (self.inicio + timedelta(hours=horas)).strftime(fmt),
        }

    def test_acepta_duracion_valida(self):
        self.assertTrue(ReservaForm(self.datos(2)).is_valid())

    def test_rechaza_duracion_excesiva(self):
        self.assertFalse(ReservaForm(self.datos(5)).is_valid())

    def test_no_se_cancela_con_menos_de_24_horas(self):
        usuario = User.objects.create_user('ana', password='1234')
        proxima = Reserva.objects.create(
            usuario=usuario, recurso=self.cancha,
            periodo=rango(timezone.now() + timedelta(hours=5)),
        )
        lejana = Reserva.objects.create(
            usuario=usuario, recurso=self.cancha,
            periodo=rango(timezone.now() + timedelta(days=3)),
        )
        self.assertFalse(proxima.puede_cancelarse())
        self.assertTrue(lejana.puede_cancelarse())


from .models import Movimiento
from .servicios import cancelar_con_devolucion, registrar_pago


class PagoTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user('ana', password='1234')
        self.cancha = Recurso.objects.create(nombre='Cancha 1', precio_hora=1000)
        self.inicio = timezone.now() + timedelta(days=3)

    def pendiente(self, vence_en=None):
        return Reserva.objects.create(
            usuario=self.usuario, recurso=self.cancha, periodo=rango(self.inicio),
            monto=1000, estado='pendiente',
            vence_en=vence_en or timezone.now() + timedelta(minutes=15),
        )

    def test_pago_confirma_y_registra_ingreso(self):
        reserva = self.pendiente()
        self.assertIsNotNone(registrar_pago(reserva, 'tarjeta'))
        reserva.refresh_from_db()
        self.assertEqual(reserva.estado, 'confirmada')
        self.assertEqual(Movimiento.objects.filter(tipo='ingreso').count(), 1)

    def test_no_se_paga_una_reserva_vencida(self):
        reserva = self.pendiente(vence_en=timezone.now() - timedelta(minutes=1))
        self.assertIsNone(registrar_pago(reserva, 'tarjeta'))

    def test_reserva_vencida_libera_el_horario(self):
        self.pendiente(vence_en=timezone.now() - timedelta(minutes=1))
        Reserva.objects.liberar_vencidas()
        Reserva.objects.create(
            usuario=self.usuario, recurso=self.cancha, periodo=rango(self.inicio)
        )
        self.assertEqual(Reserva.objects.filter(estado='confirmada').count(), 1)

    def test_cancelar_reserva_pagada_genera_devolucion(self):
        reserva = self.pendiente()
        registrar_pago(reserva, 'tarjeta')
        reserva.refresh_from_db()
        self.assertEqual(cancelar_con_devolucion(reserva), 1000)
        self.assertEqual(
            Movimiento.objects.filter(tipo='gasto', categoria='devolucion').count(), 1
        )


