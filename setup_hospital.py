import requests
import sys

BASE_URL = "http://localhost:8000"


def verificar_servidor():
    """Verifica que el servidor esté corriendo"""
    print("🔍 Verificando servidor...")
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=5)
        if response.status_code == 200:
            print("✅ Servidor respondiendo correctamente")
            return True
    except requests.exceptions.RequestException:
        print("❌ El servidor no está corriendo")
        print("\n💡 Por favor inicia el servidor primero:")
        print("   python main.py")
        return False


def inicializar_hospitales():
    """Inicializa los 3 hospitales del sistema"""
    print("\n🏥 Inicializando sistema multi-hospitalario...")
    
    hospitales = [
        {"id": "PMONTT", "nombre": "Hospital Puerto Montt"},
        {"id": "CALBUCO", "nombre": "Hospital Calbuco"},
        {"id": "LLANHUE", "nombre": "Hospital Llanquihue"},
    ]
    
    for hospital in hospitales:
        try:
            response = requests.post(
                f"{BASE_URL}/hospitales/inicializar-multi",
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                print(f"\n✅ Sistema inicializado:")
                print(f"   Total de hospitales: {data['total_hospitales']}")
                print(f"   Total de camas: {data['total_camas']}")
                for hosp_info in data['hospitales']:
                    print(f"   • {hosp_info['nombre']}: {hosp_info['camas']} camas")
                return True
            elif response.status_code == 400:
                print("ℹ️ El sistema ya está inicializado")
                return True
            else:
                print(f"❌ Error al inicializar: {response.text}")
                return False
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Error de conexión: {e}")
            return False


def mostrar_estadisticas():
    """Muestra estadísticas de todos los hospitales"""
    print("\n📊 ESTADÍSTICAS DEL SISTEMA\n")
    print("="*80)
    
    hospitales = [
        {"id": "PMONTT", "nombre": "Hospital Puerto Montt"},
        {"id": "CALBUCO", "nombre": "Hospital Calbuco"},
        {"id": "LLANHUE", "nombre": "Hospital Llanquihue"},
    ]
    
    for hospital in hospitales:
        try:
            response = requests.get(
                f"{BASE_URL}/hospitales/{hospital['id']}/estadisticas",
                timeout=10
            )
            
            if response.status_code == 200:
                stats = response.json()
                print(f"\n🏥 {hospital['nombre'].upper()}")
                print("-" * 40)
                print(f"   📈 Total de camas: {stats['total_camas']}")
                print(f"   📊 Tasa de ocupación: {stats['tasa_ocupacion']}%")
                print(f"   ⏳ Pacientes en espera: {stats['pacientes_en_espera']}")
                
                print(f"\n   Estados:")
                emojis = {
                    "libre": "⚪",
                    "ocupada": "🟢",
                    "pendiente_traslado": "🟡",
                    "en_traslado": "🟠",
                    "alta_sugerida": "🔵"
                }
                for estado, cantidad in stats['por_estado'].items():
                    if cantidad > 0:
                        emoji = emojis.get(estado, "⚫")
                        print(f"   {emoji} {estado}: {cantidad}")
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Error obteniendo estadísticas de {hospital['nombre']}: {e}")
    
    print("\n" + "="*80)


def main():
    print("="*80)
    print("  CONFIGURACIÓN INICIAL DEL SISTEMA MULTI-HOSPITALARIO")
    print("  ✅ Sistema v3.0 - Red de Hospitales")
    print("="*80)
    
    # 1. Verificar servidor
    if not verificar_servidor():
        sys.exit(1)
    
    # 2. Inicializar hospitales
    if not inicializar_hospitales():
        print("\n❌ No se pudo inicializar el sistema")
        sys.exit(1)
    
    # 3. Mostrar estadísticas
    mostrar_estadisticas()
    
    # 4. Información final
    print("\n✨ ¡Sistema multi-hospitalario listo!")
    print("\n🌐 Próximos pasos:")
    print("   1. Abre el dashboard: http://localhost:8000/dashboard")
    print("   2. Selecciona un hospital en el menú superior")
    print("   3. Registra pacientes y gestiona camas")
    print("\n📚 Hospitales disponibles:")
    print("   • Hospital Puerto Montt (PMONTT) - 30 camas")
    print("   • Hospital Calbuco (CALBUCO) - 16 camas")
    print("   • Hospital Llanquihue (LLANHUE) - 16 camas")
    print("\n" + "="*80)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️ Configuración cancelada por el usuario")
        sys.exit(0)