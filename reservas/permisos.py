from .models import Complejo


def complejos_del_usuario(usuario):
    """Complejos que el usuario puede gestionar: todos si es superusuario,
    los asignados si es staff, ninguno en otro caso."""
    if not usuario.is_authenticated or not usuario.is_staff:
        return Complejo.objects.none()
    if usuario.is_superuser:
        return Complejo.objects.all()
    return usuario.complejos_administrados.all()