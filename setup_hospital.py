#!/usr/bin/env python3
"""
Script para inicializar el sistema de gestión de camas hospitalarias.
"""

import requests
import sys

BASE_URL = "http://localhost:8000"


def verificar_servidor():
    """Verifica que el servidor esté corriendo."""
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=5)
        if response.status_code == 200:
            print("✅ Servidor conectado")
            return True
        else:
            print(f"❌ Servidor respondió con código {response.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        print("❌ No se puede conectar al servidor.")
        print("   Asegúrate de que el servidor esté corriendo:")
        print("   python main.py")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def limpiar_sistema():
    """Elimina todos los hospitales existentes."""
    print("\n🧹 Limpiando sistema existente...")
    
    try:
        # Obtener lista de hospitales
        response = requests.get(f"{BASE_URL}/hospitales")
        if response.status_code == 200:
            hospitales = response.json()
            for hospital in hospitales:
                hospital_id = hospital.get('id')
                if hospital_id:
                    del_response = requests.delete(f"{BASE_URL}/hospitales/{hospital_id}")
                    if del_response.status_code == 200:
                        print(f"   ✅ Hospital {hospital_id} eliminado")
                    else:
                        print(f"   ⚠️ No se pudo eliminar {hospital_id}")
            print("✅ Sistema limpiado")
            return True
        else:
            print("   No hay hospitales para limpiar")
            return True
    except Exception as e:
        print(f"   ⚠️ Error limpiando: {e}")
        return True  # Continuar de todos modos


def inicializar_hospitales():
    """Inicializa los hospitales del sistema."""
    print("\n🏥 Inicializando hospitales...")
    
    try:
        response = requests.post(f"{BASE_URL}/hospitales/inicializar-multi")
        
        if response.status_code == 200:
            data = response.json()
            print("✅ Sistema inicializado:")
            
            # ✅ CORREGIDO: Manejar diferentes formatos de respuesta
            total_hospitales = data.get('total_hospitales', len(data.get('hospitales', [])))
            total_camas = data.get('total_camas', 0)
            
            print(f"   Total de hospitales: {total_hospitales}")
            print(f"   Total de camas: {total_camas}")
            
            hospitales = data.get('hospitales', [])
            for h in hospitales:
                nombre = h.get('nombre', h.get('id', 'Desconocido'))
                camas = h.get('camas', 0)
                print(f"   - {nombre}: {camas} camas")
            
            return True
        elif response.status_code == 400:
            error = response.json()
            print(f"⚠️ {error.get('detail', 'Sistema ya inicializado')}")
            return True  # Ya está inicializado, no es error
        else:
            print(f"❌ Error inicializando: {response.status_code}")
            print(f"   {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def crear_pacientes_prueba():
    """Crea algunos pacientes de prueba."""
    print("\n👥 Creando pacientes de prueba...")
    
    pacientes_prueba = [
        {
            "nombre": "María González",
            "run": "12345678-9",
            "sexo": "mujer",
            "edad": 45,
            "enfermedad": "medica",
            "aislamiento": "ninguno",
            "requerimientos": ["tratamiento_endovenoso", "oxigeno_naricera"],
            "es_embarazada": False,
            "caso_sociosanitario": False,
            "espera_cardio": False
        },
        {
            "nombre": "Juan Pérez",
            "run": "98765432-1",
            "sexo": "hombre",
            "edad": 67,
            "enfermedad": "quirurgica",
            "aislamiento": "ninguno",
            "requerimientos": ["monitorizacion_continua", "drogas_vasoactivas"],
            "es_embarazada": False,
            "caso_sociosanitario": False,
            "espera_cardio": False
        },
        {
            "nombre": "Ana Muñoz",
            "run": "11111111-1",
            "sexo": "mujer",
            "edad": 32,
            "enfermedad": "obstetrica",
            "aislamiento": "ninguno",
            "requerimientos": ["tratamiento_endovenoso"],
            "es_embarazada": True,
            "caso_sociosanitario": False,
            "espera_cardio": False
        }
    ]
    
    hospital_id = "PMONTT"
    creados = 0
    
    for paciente in pacientes_prueba:
        try:
            response = requests.post(
                f"{BASE_URL}/hospitales/{hospital_id}/pacientes/ingresar",
                json=paciente
            )
            
            if response.status_code == 200:
                data = response.json()
                print(f"   ✅ {paciente['nombre']} - Prioridad: {data.get('prioridad', 'N/A')}")
                creados += 1
            else:
                print(f"   ⚠️ Error creando {paciente['nombre']}: {response.text}")
                
        except Exception as e:
            print(f"   ⚠️ Error: {e}")
    
    print(f"✅ {creados}/{len(pacientes_prueba)} pacientes creados")
    return creados > 0


def mostrar_estado():
    """Muestra el estado actual del sistema."""
    print("\n📊 Estado del sistema:")
    
    try:
        # Estadísticas de Puerto Montt
        response = requests.get(f"{BASE_URL}/hospitales/PMONTT/estadisticas")
        if response.status_code == 200:
            stats = response.json()
            print(f"\n   Hospital Puerto Montt:")
            print(f"   - Total camas: {stats.get('total_camas', 0)}")
            print(f"   - Ocupadas: {stats.get('por_estado', {}).get('ocupada', 0)}")
            print(f"   - Libres: {stats.get('por_estado', {}).get('libre', 0)}")
            print(f"   - En espera: {stats.get('pacientes_en_espera', 0)}")
            print(f"   - Tasa ocupación: {stats.get('tasa_ocupacion', 0)}%")
        
        # Cola de prioridad
        response = requests.get(f"{BASE_URL}/hospitales/PMONTT/cola-prioridad")
        if response.status_code == 200:
            data = response.json()
            pacientes = data.get('pacientes', [])
            if pacientes:
                print(f"\n   Cola de prioridad ({len(pacientes)} pacientes):")
                for i, p in enumerate(pacientes[:5], 1):
                    print(f"   {i}. {p.get('nombre', 'N/A')} - Prioridad: {p.get('prioridad', 0):.1f}")
            else:
                print("\n   Cola de prioridad: vacía")
                
    except Exception as e:
        print(f"   ⚠️ Error obteniendo estado: {e}")


def main():
    """Función principal."""
    print("=" * 60)
    print("🏥 SETUP - Sistema de Gestión de Camas Hospitalarias")
    print("=" * 60)
    
    # Verificar conexión
    if not verificar_servidor():
        sys.exit(1)
    
    # Preguntar si limpiar
    respuesta = input("\n¿Limpiar sistema existente? (s/N): ").strip().lower()
    if respuesta == 's':
        limpiar_sistema()
    
    # Inicializar hospitales
    if not inicializar_hospitales():
        print("\n❌ Error inicializando el sistema")
        sys.exit(1)
    
    # Preguntar si crear pacientes de prueba
    respuesta = input("\n¿Crear pacientes de prueba? (s/N): ").strip().lower()
    if respuesta == 's':
        crear_pacientes_prueba()
    
    # Mostrar estado
    mostrar_estado()
    
    print("\n" + "=" * 60)
    print("✅ Setup completado")
    print("   Dashboard: http://localhost:8000/dashboard")
    print("   API Docs: http://localhost:8000/docs")
    print("=" * 60)


if __name__ == "__main__":
    main()