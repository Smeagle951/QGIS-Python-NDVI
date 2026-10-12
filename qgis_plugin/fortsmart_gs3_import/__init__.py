def classFactory(iface):
    from .plugin import FortSmartGs3ImportPlugin
    return FortSmartGs3ImportPlugin(iface)
