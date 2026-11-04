def pesos(valor):
    texto = f'{valor:,.2f}'
    return '$' + texto.replace(',', '_').replace('.', ',').replace('_', '.')