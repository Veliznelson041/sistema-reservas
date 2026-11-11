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


from .models import Complejo


def complejo_demo():
    return Complejo.objects.get_or_create(
        slug='demo', defaults={'nombre': 'Complejo Demo'}
    )[0]


def nuevo_recurso(**kwargs):
    if 'complejo' not in kwargs:
        kwargs['complejo'] = complejo_demo()
    recurso = Recurso(**kwargs)
    recurso.save()
    return recurso


def nuevo_admin(username, password='1234', complejo=None):
    """Usuario staff asignado a un complejo (por defecto, el demo)."""
    usuario = User.objects.create_user(username, password=password, is_staff=True)
    (complejo or complejo_demo()).administradores.add(usuario)
    return usuario


class ReservaSolapamientoTests(TestCase):
    """Pruebas secuenciales de la restricción de exclusión."""

    def setUp(self):
        self.usuario = User.objects.create_user('nelson', password='1234')
        self.cancha1 = nuevo_recurso(nombre='Cancha 1')
        self.cancha2 = nuevo_recurso(nombre='Cancha 2')
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
        cancha = nuevo_recurso(nombre='Cancha 1')
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
        self.cancha = nuevo_recurso(nombre='Cancha 1')
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
        self.cancha = nuevo_recurso(nombre='Cancha 1', precio_hora=1000)
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
        self.admin = nuevo_admin('adm')

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
        cancha = nuevo_recurso(nombre='Cancha 1', precio_hora=1000)
        self.client.login(username='adm', password='1234')
        inicio = (timezone.localtime() + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        fmt = '%Y-%m-%dT%H:%M'
        respuesta = self.client.post('/panel/mostrador/', {
            'cliente': 'Juan Pérez', 'recurso': cancha.pk,
            'cobro': 'total', 'metodo': 'efectivo',
            'inicio': inicio.strftime(fmt),
            'fin': (inicio + timedelta(hours=1)).strftime(fmt),
        })
        self.assertEqual(respuesta.status_code, 302)
        ingresos = Movimiento.objects.filter(tipo='ingreso')
        self.assertEqual(sum(m.monto for m in ingresos), 1000)
        reserva = Reserva.objects.get()
        self.assertEqual(reserva.cliente, 'Juan Pérez')
        self.assertEqual(reserva.saldo, 0)


from .models import Pago


class FinalizadasYPdfTests(TestCase):
    def setUp(self):
        self.admin = nuevo_admin('adm')
        self.cancha = nuevo_recurso(nombre='Cancha 1', precio_hora=1000)
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
        self.admin = nuevo_admin('adm')
        self.ana = User.objects.create_user('ana', password='1234')
        self.beto = User.objects.create_user('beto', password='1234')
        self.cancha = nuevo_recurso(nombre='Cancha 1', precio_hora=1000)
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
        cancha = nuevo_recurso(nombre='Cancha 1', precio_hora=1000)
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
        self.admin = nuevo_admin('adm')
        self.ana = User.objects.create_user('ana', password='1234')
        self.beto = User.objects.create_user('beto', password='1234')
        cancha = nuevo_recurso(nombre='Cancha 1', precio_hora=1000)
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
        self.assertIn('Transferencia bancaria', d['Método de pago'])

    def test_cliente_ve_el_metodo_de_su_propia_reserva(self):
        d = self.detalle('ana', self.de_ana)
        self.assertIn('Transferencia bancaria', d['Método de pago'])

    def test_cliente_no_recibe_datos_de_otros(self):
        d = self.detalle('ana', self.de_beto)
        self.assertNotIn('Método de pago', d)
        self.assertNotIn('Cliente', d)
        respuesta = self.client.get('/calendario/eventos/').content.decode()
        self.assertNotIn('beto', respuesta)
        self.assertNotIn('Tarjeta', respuesta)

from decimal import Decimal

from .servicios import cobrar_saldo


class SenaTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user('ana', password='1234')
        self.cancha = nuevo_recurso(nombre='Cancha 1', precio_hora=1000)  # seña 30 %
        self.inicio = timezone.now() + timedelta(days=3)

    def crear(self, estado='pendiente', inicio=None):
        inicio = inicio or self.inicio
        monto = self.cancha.calcular_monto(inicio, inicio + timedelta(hours=1))
        return Reserva.objects.create(
            usuario=self.usuario, recurso=self.cancha, periodo=rango(inicio),
            monto=monto, sena=self.cancha.calcular_sena(monto), estado=estado,
            vence_en=(timezone.now() + timedelta(minutes=15)
                      if estado == 'pendiente' else None),
        )

    def test_la_sena_es_el_porcentaje_del_total(self):
        reserva = self.crear()
        self.assertEqual(reserva.monto, Decimal('1000.00'))
        self.assertEqual(reserva.sena, Decimal('300.00'))

    def test_el_pago_online_cobra_solo_la_sena(self):
        reserva = self.crear()
        pago = registrar_pago(reserva, 'tarjeta')
        reserva.refresh_from_db()
        self.assertEqual(pago.monto, Decimal('300.00'))
        self.assertEqual(reserva.estado, 'confirmada')
        self.assertEqual(reserva.saldo, Decimal('700.00'))
        ingreso = Movimiento.objects.get(tipo='ingreso')
        self.assertEqual((ingreso.categoria, ingreso.monto), ('sena', Decimal('300.00')))

    def test_cobrar_el_saldo_completa_el_pago_una_sola_vez(self):
        reserva = self.crear()
        registrar_pago(reserva, 'tarjeta')
        pago = cobrar_saldo(reserva, 'efectivo')
        self.assertEqual(pago.monto, Decimal('700.00'))
        reserva.refresh_from_db()
        self.assertEqual(reserva.saldo, 0)
        total = sum(m.monto for m in Movimiento.objects.filter(tipo='ingreso'))
        self.assertEqual(total, Decimal('1000.00'))
        self.assertIsNone(cobrar_saldo(reserva, 'efectivo'))  # no se cobra dos veces

    def test_el_saldo_se_cobra_aunque_el_turno_ya_finalizo(self):
        reserva = self.crear(
            estado='confirmada', inicio=timezone.now() - timedelta(days=2)
        )
        Pago.objects.create(reserva=reserva, monto=300, metodo='tarjeta')
        self.assertTrue(reserva.finalizada)
        self.assertIsNotNone(cobrar_saldo(reserva, 'efectivo'))

    def test_no_se_cobra_saldo_de_una_reserva_cancelada(self):
        self.assertIsNone(cobrar_saldo(self.crear(estado='cancelada'), 'efectivo'))

    def test_cancelar_a_tiempo_devuelve_solo_la_sena(self):
        reserva = self.crear()
        registrar_pago(reserva, 'tarjeta')
        reserva.refresh_from_db()
        self.assertEqual(cancelar_con_devolucion(reserva), Decimal('300.00'))
        self.assertEqual(Movimiento.objects.get(tipo='gasto').monto, Decimal('300.00'))


class CobroEnPanelTests(TestCase):
    def test_admin_cobra_el_saldo_y_el_cliente_no_puede(self):
        nuevo_admin('adm')
        cliente = User.objects.create_user('cli', password='1234')
        cancha = nuevo_recurso(nombre='Cancha 1', precio_hora=1000)
        reserva = Reserva.objects.create(
            usuario=cliente, recurso=cancha, monto=1000, sena=300, estado='confirmada',
            periodo=rango(timezone.now() - timedelta(hours=3)),
        )
        Pago.objects.create(reserva=reserva, monto=300, metodo='tarjeta')
        url = f'/panel/reservas/{reserva.pk}/cobrar/'

        self.client.login(username='cli', password='1234')
        self.assertEqual(self.client.post(url, {'metodo': 'efectivo'}).status_code, 403)

        self.client.login(username='adm', password='1234')
        r = self.client.get('/panel/reservas/?estado=saldo')
        self.assertEqual([x.pk for x in r.context['reservas']], [reserva.pk])

        self.assertEqual(self.client.post(url, {'metodo': 'efectivo'}).status_code, 302)
        reserva.refresh_from_db()
        self.assertEqual(reserva.saldo, 0)
        self.assertEqual(Movimiento.objects.get(categoria='saldo').monto, Decimal('700.00'))
        r = self.client.get('/panel/reservas/?estado=saldo')
        self.assertEqual(len(r.context['reservas']), 0)


class PaginacionTests(TestCase):
    def setUp(self):
        self.admin = nuevo_admin('adm')
        cancha = nuevo_recurso(nombre='Cancha 1', precio_hora=1000)
        base = timezone.now() + timedelta(days=1)
        for i in range(7):
            Reserva.objects.create(
                usuario=self.admin, recurso=cancha, monto=1000, estado='confirmada',
                periodo=rango(base + timedelta(hours=2 * i)),
            )
        hoy = timezone.localdate()
        for i in range(7):
            Movimiento.objects.create(complejo=complejo_demo(), 
                tipo='gasto', categoria='otros', concepto=f'Gasto {i}',
                monto=10, fecha=hoy,
            )
        self.client.login(username='adm', password='1234')

    def test_reservas_de_a_cinco_por_pagina(self):
        r = self.client.get('/panel/reservas/')
        self.assertEqual(len(r.context['reservas']), 5)
        r = self.client.get('/panel/reservas/?pagina=2')
        self.assertEqual(len(r.context['reservas']), 2)

    def test_pagina_invalida_no_rompe(self):
        for p in ['0', '99', 'abc']:
            self.assertEqual(self.client.get(f'/panel/reservas/?pagina={p}').status_code, 200)

    def test_los_filtros_se_mantienen_al_cambiar_de_pagina(self):
        r = self.client.get('/panel/reservas/?estado=confirmada')
        self.assertContains(r, 'estado=confirmada&amp;pagina=2')

    def test_movimientos_paginados(self):
        r = self.client.get('/panel/finanzas/')
        self.assertEqual(len(r.context['movimientos']), 5)
        self.assertEqual(r.context['movimientos'].paginator.count, 7)
        r = self.client.get('/panel/finanzas/?pagina=2')
        self.assertEqual(len(r.context['movimientos']), 2)

    def test_desglose_paginado(self):
        hoy = timezone.localdate()
        for cat in ['mantenimiento', 'servicios', 'sueldos', 'insumos', 'devolucion', 'reserva']:
            Movimiento.objects.create(complejo=complejo_demo(), 
                tipo='gasto', categoria=cat, concepto='x', monto=5, fecha=hoy
            )
        r = self.client.get('/panel/finanzas/')
        self.assertEqual(len(r.context['desglose']), 5)
        self.assertEqual(r.context['desglose'].paginator.count, 7)
        r = self.client.get('/panel/finanzas/?pagina_desglose=2')
        self.assertEqual(len(r.context['desglose']), 2)

    def test_cobrar_vuelve_a_la_pagina_de_origen(self):
        r1, r2 = list(Reserva.objects.order_by('pk')[:2])
        r = self.client.post(f'/panel/reservas/{r1.pk}/cobrar/', {
            'metodo': 'efectivo', 'next': '/panel/reservas/?pagina=2',
        })
        self.assertRedirects(r, '/panel/reservas/?pagina=2')
        # una URL de otro sitio se ignora
        r = self.client.post(f'/panel/reservas/{r2.pk}/cobrar/', {
            'metodo': 'efectivo', 'next': 'https://sitio-externo.com/',
        })
        self.assertEqual(r['Location'], '/panel/reservas/')


from .forms import ReservaForm


class AislamientoEntreComplejosTests(TestCase):
    def setUp(self):
        self.a = Complejo.objects.create(nombre='Complejo A', slug='a')
        self.b = Complejo.objects.create(
            nombre='Complejo B', slug='b', duracion_max_horas=2,
            hora_apertura=10, hora_cierre=22,
        )
        self.admin_a = nuevo_admin('admin_a', complejo=self.a)
        self.admin_b = nuevo_admin('admin_b', complejo=self.b)
        User.objects.create_superuser('root', password='1234')
        User.objects.create_user('suelto', password='1234', is_staff=True)
        self.cliente = User.objects.create_user('cli', password='1234')
        self.cancha_a = nuevo_recurso(nombre='Cancha A', precio_hora=1000, complejo=self.a)
        self.cancha_b = nuevo_recurso(nombre='Cancha B', precio_hora=1000, complejo=self.b)

        ahora = timezone.now()

        def reserva(cancha, dias, complejo, monto):
            r = Reserva.objects.create(
                usuario=self.cliente, recurso=cancha, monto=1000, sena=300,
                estado='confirmada', periodo=rango(ahora + timedelta(days=dias)),
            )
            Pago.objects.create(reserva=r, monto=300, metodo='tarjeta')
            Movimiento.objects.create(
                complejo=complejo, tipo='ingreso', categoria='sena',
                concepto='seña', monto=monto, reserva=r,
            )
            return r

        self.res_a = reserva(self.cancha_a, 2, self.a, 300)
        self.res_b = reserva(self.cancha_b, 3, self.b, 500)

    def test_staff_sin_complejo_no_entra_al_panel(self):
        self.client.login(username='suelto', password='1234')
        self.assertEqual(self.client.get('/panel/').status_code, 403)

    def test_admin_solo_ve_las_reservas_de_su_complejo(self):
        self.client.login(username='admin_a', password='1234')
        r = self.client.get('/panel/reservas/')
        self.assertEqual([x.pk for x in r.context['reservas']], [self.res_a.pk])

    def test_admin_no_puede_cancelar_ni_cobrar_en_otro_complejo(self):
        self.client.login(username='admin_a', password='1234')
        r = self.client.post(f'/panel/reservas/{self.res_b.pk}/cancelar/')
        self.assertEqual(r.status_code, 404)
        r = self.client.post(
            f'/panel/reservas/{self.res_b.pk}/cobrar/', {'metodo': 'efectivo'}
        )
        self.assertEqual(r.status_code, 404)
        self.res_b.refresh_from_db()
        self.assertEqual(self.res_b.estado, 'confirmada')
        self.assertEqual(Movimiento.objects.filter(categoria='saldo').count(), 0)

    def test_admin_si_gestiona_su_propio_complejo(self):
        self.client.login(username='admin_a', password='1234')
        r = self.client.post(
            f'/panel/reservas/{self.res_a.pk}/cobrar/', {'metodo': 'efectivo'}
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Movimiento.objects.filter(categoria='saldo').count(), 1)

    def test_finanzas_solo_del_complejo(self):
        for usuario, esperado in [('admin_a', 300), ('admin_b', 500), ('root', 800)]:
            self.client.login(username=usuario, password='1234')
            r = self.client.get('/panel/finanzas/')
            self.assertEqual(r.context['kpis']['ingresos'], esperado, usuario)

    def test_no_se_elimina_un_movimiento_de_otro_complejo(self):
        gasto = Movimiento.objects.create(
            complejo=self.b, tipo='gasto', categoria='otros', concepto='luz', monto=10
        )
        self.client.login(username='admin_a', password='1234')
        r = self.client.post(f'/panel/finanzas/{gasto.pk}/eliminar/')
        self.assertEqual(r.status_code, 404)
        self.assertTrue(Movimiento.objects.filter(pk=gasto.pk).exists())

    def test_superusuario_filtra_con_el_selector(self):
        self.client.login(username='root', password='1234')
        r = self.client.get('/panel/reservas/')
        self.assertEqual(len(r.context['reservas']), 2)
        self.client.post('/panel/complejo/', {'complejo': self.a.pk})
        r = self.client.get('/panel/reservas/')
        self.assertEqual([x.pk for x in r.context['reservas']], [self.res_a.pk])

    def test_un_admin_no_puede_elegir_un_complejo_ajeno(self):
        self.client.login(username='admin_a', password='1234')
        self.client.post('/panel/complejo/', {'complejo': self.b.pk})
        r = self.client.get('/panel/reservas/')
        self.assertEqual([x.pk for x in r.context['reservas']], [self.res_a.pk])

    def test_calendario_no_filtra_datos_de_otros_complejos(self):
        self.client.login(username='admin_a', password='1234')
        eventos = {e['id']: e for e in self.client.get('/calendario/eventos/').json()}
        propia = dict(eventos[self.res_a.pk]['extendedProps']['detalle'])
        ajena = dict(eventos[self.res_b.pk]['extendedProps']['detalle'])
        self.assertIn('Cliente', propia)
        self.assertNotIn('Cliente', ajena)
        self.assertNotIn('Método de pago', ajena)
        self.assertEqual(eventos[self.res_b.pk]['color'], '#94a3b8')


class ReglasPorComplejoTests(TestCase):
    def setUp(self):
        self.a = Complejo.objects.create(nombre='Complejo A', slug='a')   # 8-24 h, máx 3 h
        self.b = Complejo.objects.create(
            nombre='Complejo B', slug='b', duracion_max_horas=2,
            hora_apertura=10, hora_cierre=22,
        )
        self.cancha_a = nuevo_recurso(nombre='Cancha A', precio_hora=1000, complejo=self.a)
        self.cancha_b = nuevo_recurso(nombre='Cancha B', precio_hora=1000, complejo=self.b)

    def datos(self, cancha, hora, horas, dias=2):
        base = (timezone.localtime() + timedelta(days=dias)).replace(
            hour=hora, minute=0, second=0, microsecond=0
        )
        fmt = '%Y-%m-%dT%H:%M'
        return {
            'recurso': cancha.pk,
            'inicio': base.strftime(fmt),
            'fin': (base + timedelta(hours=horas)).strftime(fmt),
        }

    def test_cada_complejo_aplica_su_duracion_maxima(self):
        self.assertTrue(ReservaForm(self.datos(self.cancha_a, 10, 3)).is_valid())
        self.assertFalse(ReservaForm(self.datos(self.cancha_b, 10, 3)).is_valid())
        self.assertTrue(ReservaForm(self.datos(self.cancha_b, 10, 2)).is_valid())

    def test_cada_complejo_aplica_su_horario(self):
        self.assertTrue(ReservaForm(self.datos(self.cancha_a, 9, 1)).is_valid())
        self.assertFalse(ReservaForm(self.datos(self.cancha_b, 9, 1)).is_valid())    # abre a las 10
        self.assertTrue(ReservaForm(self.datos(self.cancha_b, 21, 1)).is_valid())    # termina 22:00
        self.assertFalse(ReservaForm(self.datos(self.cancha_b, 21, 2)).is_valid())   # pasa del cierre

    def test_se_puede_terminar_justo_a_medianoche_si_el_complejo_cierra_a_las_24(self):
        self.assertTrue(ReservaForm(self.datos(self.cancha_a, 23, 1)).is_valid())

    def test_la_cancelacion_usa_el_plazo_del_complejo(self):
        self.b.anticipacion_cancelacion_horas = 72
        self.b.save()
        usuario = User.objects.create_user('ana', password='1234')
        en_dos_dias = timezone.now() + timedelta(days=2)
        en_a = Reserva.objects.create(
            usuario=usuario, recurso=self.cancha_a, monto=1000, estado='confirmada',
            periodo=rango(en_dos_dias),
        )
        en_b = Reserva.objects.create(
            usuario=usuario, recurso=self.cancha_b, monto=1000, estado='confirmada',
            periodo=rango(en_dos_dias),
        )
        self.assertTrue(en_a.puede_cancelarse())    # plazo 24 h
        self.assertFalse(en_b.puede_cancelarse())   # plazo 72 h

