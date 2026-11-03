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


class PanelTests(TestCase):
    def setUp(self):
        self.cliente = User.objects.create_user('cli', password='1234')
        self.admin = User.objects.create_user('adm', password='1234', is_staff=True)

    def test_anonimo_va_al_login(self):
        self.assertEqual(self.client.get('/panel/').status_code, 302)

    def test_cliente_no_entra_al_panel(self):
        self.client.login(username='cli', password='1234')
        self.assertEqual(self.client.get('/panel/').status_code, 403)

    def test_admin_ve_todas_las_secciones(self):
        self.client.login(username='adm', password='1234')
        for url in ['/panel/', '/panel/reservas/', '/panel/mostrador/', '/panel/finanzas/']:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_mostrador_registra_reserva_y_cobro(self):
        cancha = Recurso.objects.create(nombre='Cancha 1', precio_hora=1000)
        self.client.login(username='adm', password='1234')
        inicio = (timezone.localtime() + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        fmt = '%Y-%m-%dT%H:%M'
        respuesta = self.client.post('/panel/mostrador/', {
            'cliente': 'Juan Pérez', 'recurso': cancha.pk, 'metodo': 'efectivo',
            'inicio': inicio.strftime(fmt),
            'fin': (inicio + timedelta(hours=1)).strftime(fmt),
        })
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(Movimiento.objects.filter(tipo='ingreso', monto=1000).count(), 1)
        self.assertEqual(Reserva.objects.get().cliente, 'Juan Pérez')


from .models import Pago


class FinalizadasYPdfTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('adm', password='1234', is_staff=True)
        self.cancha = Recurso.objects.create(nombre='Cancha 1', precio_hora=1000)
        self.pasada = Reserva.objects.create(
            usuario=self.admin, recurso=self.cancha, monto=1000, estado='confirmada',
            periodo=rango(timezone.now() - timedelta(days=2)),
        )
        Pago.objects.create(reserva=self.pasada, monto=1000, metodo='tarjeta')
        self.futura = Reserva.objects.create(
            usuario=self.admin, recurso=self.cancha, monto=1000, estado='confirmada',
            periodo=rango(timezone.now() + timedelta(days=3)),
        )
        self.client.login(username='adm', password='1234')

    def test_reserva_pasada_figura_como_finalizada(self):
        self.assertTrue(self.pasada.finalizada)
        self.assertEqual(self.pasada.estado_texto, 'Finalizada')
        self.assertFalse(self.futura.finalizada)

    def test_no_se_cancela_ni_devuelve_una_finalizada(self):
        self.client.post(f'/panel/reservas/{self.pasada.pk}/cancelar/')
        self.pasada.refresh_from_db()
        self.assertEqual(self.pasada.estado, 'confirmada')
        self.assertEqual(Movimiento.objects.filter(tipo='gasto').count(), 0)

    def test_filtros_de_finalizada_y_confirmada(self):
        r = self.client.get('/panel/reservas/?estado=finalizada')
        self.assertEqual([x.pk for x in r.context['reservas']], [self.pasada.pk])
        r = self.client.get('/panel/reservas/?estado=confirmada')
        self.assertEqual([x.pk for x in r.context['reservas']], [self.futura.pk])

    def test_pdf_de_finanzas(self):
        r = self.client.get('/panel/finanzas/pdf/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertTrue(r.content.startswith(b'%PDF'))


class CalendarioColoresTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('adm', password='1234', is_staff=True)
        self.ana = User.objects.create_user('ana', password='1234')
        self.beto = User.objects.create_user('beto', password='1234')
        self.cancha = Recurso.objects.create(nombre='Cancha 1', precio_hora=1000)
        ahora = timezone.now()

        def crear(usuario, dias, estado, vence=None):
            return Reserva.objects.create(
                usuario=usuario, recurso=self.cancha, monto=1000, estado=estado,
                vence_en=vence, periodo=rango(ahora + timedelta(days=dias)),
            )

        self.finalizada = crear(self.ana, -3, 'confirmada')
        self.pagada = crear(self.ana, 2, 'confirmada')
        self.pendiente = crear(self.beto, 4, 'pendiente', ahora + timedelta(minutes=10))
        self.cancelada = crear(self.beto, 6, 'cancelada')
        self.vencida = crear(self.beto, 8, 'pendiente', ahora - timedelta(minutes=1))

    def colores(self, usuario):
        self.client.login(username=usuario, password='1234')
        datos = self.client.get('/calendario/eventos/').json()
        return {e['id']: e['color'] for e in datos}

    def test_admin_ve_cada_estado_con_su_color(self):
        c = self.colores('adm')
        self.assertEqual(c, {
            self.finalizada.pk: '#dc2626',
            self.pagada.pk: '#16a34a',
            self.pendiente.pk: '#facc15',
            self.cancelada.pk: '#f97316',
        })  # la vencida no aparece: el horario quedó libre

    def test_cliente_ve_las_suyas_en_violeta_y_las_ajenas_en_gris(self):
        c = self.colores('ana')
        self.assertEqual(c, {
            self.finalizada.pk: '#4f46e5',
            self.pagada.pk: '#4f46e5',
            self.pendiente.pk: '#94a3b8',
        })  # sin canceladas ni vencidas


class CancelarFinalizadaTests(TestCase):
    def test_el_servicio_rechaza_una_reserva_finalizada(self):
        usuario = User.objects.create_user('ana', password='1234')
        cancha = Recurso.objects.create(nombre='Cancha 1', precio_hora=1000)
        pasada = Reserva.objects.create(
            usuario=usuario, recurso=cancha, monto=1000, estado='confirmada',
            periodo=rango(timezone.now() - timedelta(days=2)),
        )
        Pago.objects.create(reserva=pasada, monto=1000, metodo='tarjeta')
        with self.assertRaises(ValueError):
            cancelar_con_devolucion(pasada)
        self.assertEqual(Movimiento.objects.filter(tipo='gasto').count(), 0)

class DetalleCalendarioTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('adm', password='1234', is_staff=True)
        self.ana = User.objects.create_user('ana', password='1234')
        self.beto = User.objects.create_user('beto', password='1234')
        cancha = Recurso.objects.create(nombre='Cancha 1', precio_hora=1000)
        ahora = timezone.now()
        self.de_ana = Reserva.objects.create(
            usuario=self.ana, recurso=cancha, monto=1000, estado='confirmada',
            periodo=rango(ahora + timedelta(days=2)),
        )
        Pago.objects.create(reserva=self.de_ana, monto=1000, metodo='transferencia')
        self.de_beto = Reserva.objects.create(
            usuario=self.beto, recurso=cancha, monto=1000, estado='confirmada',
            periodo=rango(ahora + timedelta(days=4)),
        )
        Pago.objects.create(reserva=self.de_beto, monto=1000, metodo='tarjeta')

    def detalle(self, usuario, reserva):
        self.client.login(username=usuario, password='1234')
        eventos = self.client.get('/calendario/eventos/').json()
        evento = next(e for e in eventos if e['id'] == reserva.pk)
        return dict(evento['extendedProps']['detalle'])

    def test_admin_ve_cliente_cancha_y_metodo(self):
        d = self.detalle('adm', self.de_ana)
        self.assertEqual(d['Cliente'], 'ana')
        self.assertEqual(d['Cancha'], 'Cancha 1')
        self.assertEqual(d['Método de pago'], 'Transferencia bancaria')

    def test_cliente_ve_el_metodo_de_su_propia_reserva(self):
        d = self.detalle('ana', self.de_ana)
        self.assertEqual(d['Método de pago'], 'Transferencia bancaria')

    def test_cliente_no_recibe_datos_de_otros(self):
        d = self.detalle('ana', self.de_beto)
        self.assertNotIn('Método de pago', d)
        self.assertNotIn('Cliente', d)
        respuesta = self.client.get('/calendario/eventos/').content.decode()
        self.assertNotIn('beto', respuesta)
        self.assertNotIn('Tarjeta', respuesta)
