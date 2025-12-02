"""
MAIN.PY - API Principal del Sistema de Gestión de Camas Hospitalarias

Implementa:
- Sistema de asignación automática activa en segundo plano
- Cola de prioridad global para pacientes que requieren nueva cama
- Endpoints REST para gestión de hospitales, camas y pacientes
- WebSocket para actualizaciones en tiempo real
"""

from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.encoders import jsonable_encoder
from sqlmodel import SQLModel, create_engine, Session, select
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Set
from datetime import datetime
import os
import json
import uuid
import asyncio

# Intentar importar Redis
try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    print("⚠️ Redis no está instalado.")

from models import (
    Hospital, Cama, Paciente,
    ServicioEnum, EstadoCamaEnum, SexoEnum, EdadCategoriaEnum,
    EnfermedadEnum, AislamientoEnum, ComplejidadEnum, TipoPacienteEnum,
    get_configuracion_inicial_camas,
    determinar_categoria_edad,
    calcular_puntos_complejidad,
    tiene_requerimientos_uci,
    tiene_requerimientos_uti,
    determinar_complejidad_por_puntos
)

from logic import (
    asignar_cama,
    actualizar_complejidad_paciente,
    determinar_servicio_requerido,
    requiere_cambio_cama,
    requiere_alta,
    actualizar_sexo_sala,
    liberar_sexo_sala,
    priorizar_pacientes,
    asignar_camas_batch,
    buscar_candidatos_cama,
    buscar_cama_para_paciente,
    descartar_salas_sexo_incompatible,
    filtrar_por_servicio,
    filtrar_por_aislamiento,
    priorizar_camas,
    puede_asignar_sala_compartida
)

from cola_prioridad import (
    gestor_colas_global,
    calcular_prioridad_paciente,
    explicar_prioridad,
)


# ============================================
# CONFIGURACIÓN
# ============================================

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./hospital.db")
connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
engine = create_engine(DATABASE_URL, echo=False, connect_args=connect_args)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
redis_client = None

# Intervalo de asignación automática (segundos)
INTERVALO_ASIGNACION_AUTOMATICA = 5

# Flag para controlar el sistema de asignación automática
_asignacion_activa = True


# ============================================
# SISTEMA DE ASIGNACIÓN AUTOMÁTICA
# ============================================

async def proceso_asignacion_automatica():
    """
    Proceso en segundo plano que asigna camas automáticamente.
    ✅ CORREGIDO: Mejor manejo de duplicados y verificaciones.
    """
    global _asignacion_activa
    
    print("🚀 Proceso de asignación automática iniciado")
    
    while _asignacion_activa:
        try:
            await asyncio.sleep(INTERVALO_ASIGNACION_AUTOMATICA)
            
            if not _asignacion_activa:
                break
            
            # Procesar cada hospital
            with Session(engine) as session:
                query_hospitales = select(Hospital)
                hospitales = session.exec(query_hospitales).all()
                
                for hospital in hospitales:
                    await procesar_cola_hospital(hospital.id, session)
                    
        except Exception as e:
            print(f"❌ Error en proceso de asignación automática: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)  # Esperar antes de reintentar
    
    print("🛑 Proceso de asignación automática detenido")


async def procesar_cola_hospital(hospital_id: str, session: Session):
    """
    Procesa la cola de prioridad de un hospital específico.
    ✅ CORREGIDO: Filtrado correcto de camas y validaciones.
    """
    cola = gestor_colas_global.obtener_cola(hospital_id)
    
    if cola.esta_vacio():
        return
    
    # ✅ CORRECCIÓN: Obtener solo camas realmente LIBRES (no PENDIENTE_TRASLADO)
    camas_libres = session.exec(
        select(Cama).where(
            Cama.hospital_id == hospital_id,
            Cama.estado == EstadoCamaEnum.LIBRE  # Solo LIBRE, no PENDIENTE
        )
    ).all()
    
    if not camas_libres:
        print(f"ℹ️ No hay camas libres en {hospital_id}")
        return
    
    # Obtener todas las camas del hospital
    todas_camas = session.exec(
        select(Cama).where(Cama.hospital_id == hospital_id)
    ).all()
    
    # Obtener lista de pacientes ordenados
    lista_pacientes_info = cola.obtener_lista_ordenada()
    
    pacientes_procesados = 0
    max_asignaciones_por_ciclo = 5  # Límite para evitar bucles infinitos
    
    for info_paciente in lista_pacientes_info:
        if pacientes_procesados >= max_asignaciones_por_ciclo:
            print(f"ℹ️ Límite de asignaciones alcanzado en este ciclo ({max_asignaciones_por_ciclo})")
            break
        
        # Extraer ID del diccionario
        if isinstance(info_paciente, dict):
            paciente_id = info_paciente.get("paciente_id")
        else:
            paciente_id = info_paciente
        
        if not paciente_id or not isinstance(paciente_id, str):
            print(f"⚠️ Paciente ID inválido: {info_paciente}")
            continue
        
        # ✅ ACTUALIZAR lista de camas libres en cada iteración
        camas_libres_actuales = [c for c in todas_camas if c.estado == EstadoCamaEnum.LIBRE]
        if not camas_libres_actuales:
            print(f"ℹ️ No quedan camas libres en {hospital_id}")
            break
        
        # Obtener paciente
        try:
            paciente = session.get(Paciente, paciente_id)
        except Exception as e:
            print(f"❌ Error obteniendo paciente {paciente_id}: {e}")
            gestor_colas_global.remover_paciente(paciente_id, hospital_id)
            continue
        
        if not paciente:
            print(f"⚠️ Paciente {paciente_id} no encontrado")
            gestor_colas_global.remover_paciente(paciente_id, hospital_id)
            continue
        
        # ✅ VALIDACIÓN: Verificar si ya tiene cama destino asignada
        if paciente.cama_destino_id:
            print(f"ℹ️ Paciente {paciente.nombre} ya tiene cama destino: {paciente.cama_destino_id}")
            gestor_colas_global.remover_paciente(paciente_id, hospital_id)
            continue
        
        # ✅ VALIDACIÓN: Verificar si realmente está en lista de espera
        if not paciente.en_lista_espera:
            print(f"⚠️ Paciente {paciente.nombre} ya no está en lista de espera")
            gestor_colas_global.remover_paciente(paciente_id, hospital_id)
            continue
        
        # Buscar cama compatible
        try:
            cama_asignada = buscar_cama_para_paciente(paciente, camas_libres_actuales, todas_camas)
        except Exception as e:
            print(f"❌ Error buscando cama para {paciente.nombre}: {e}")
            continue
        
        if cama_asignada:
            # Realizar asignación
            exito = await realizar_asignacion_automatica(paciente, cama_asignada, session, hospital_id)
            
            if exito:
                print(f"✅ Asignación automática exitosa: {paciente.nombre} → {cama_asignada.id}")
                pacientes_procesados += 1
                
                # Actualizar todas_camas después de asignación exitosa
                todas_camas = session.exec(
                    select(Cama).where(Cama.hospital_id == hospital_id)
                ).all()
            else:
                print(f"⚠️ No se pudo asignar cama a {paciente.nombre}")


async def realizar_asignacion_automatica(
    paciente: Paciente,
    cama: Cama,
    session: Session,
    hospital_id: str
):
    """
    Realiza la asignación automática de un paciente a una cama.
    ✅ CORREGIDO: Verificación de estados y manejo de cama origen.
    """
    try:
        # ✅ VALIDACIÓN 1: Verificar que la cama esté realmente disponible
        if cama.estado not in [EstadoCamaEnum.LIBRE, EstadoCamaEnum.PENDIENTE_TRASLADO]:
            print(f"⚠️ Cama {cama.id} no disponible (estado: {cama.estado.value})")
            return False
        
        # ✅ VALIDACIÓN 2: Si la cama ya está PENDIENTE_TRASLADO para ESTE paciente, no hacer nada
        if cama.estado == EstadoCamaEnum.PENDIENTE_TRASLADO and cama.paciente_id == paciente.id:
            print(f"ℹ️ Cama {cama.id} ya asignada a {paciente.nombre}, saltando asignación")
            return True  # Ya está asignada correctamente
        
        # ✅ VALIDACIÓN 3: Si la cama está PENDIENTE_TRASLADO para OTRO paciente, no usar
        if cama.estado == EstadoCamaEnum.PENDIENTE_TRASLADO and cama.paciente_id != paciente.id:
            print(f"⚠️ Cama {cama.id} ya reservada para otro paciente")
            return False
        
        # Obtener todas las camas para actualizar sexo de sala
        todas_camas = session.exec(
            select(Cama).where(Cama.hospital_id == hospital_id)
        ).all()
        
        # Actualizar sexo de sala si es compartida
        actualizar_sexo_sala(cama, paciente, todas_camas, session)
        
        # ✅ PASO 1: Manejar cama de origen si existe
        if paciente.cama_id:
            cama_origen = session.get(Cama, paciente.cama_id)
            if cama_origen:
                # Marcar cama origen como EN_TRASLADO (naranja)
                cama_origen.estado = EstadoCamaEnum.EN_TRASLADO
                session.add(cama_origen)
                print(f"📤 Cama origen {cama_origen.id} marcada EN_TRASLADO")
        
        # ✅ PASO 2: Asignar cama destino
        cama.estado = EstadoCamaEnum.PENDIENTE_TRASLADO
        cama.paciente_id = paciente.id
        
        # ✅ PASO 3: Actualizar paciente
        paciente.cama_destino_id = cama.id
        paciente.en_lista_espera = False  # Sacar de lista de espera
        paciente.en_espera = True  # Sigue esperando traslado físico
        paciente.tipo_paciente = TipoPacienteEnum.PENDIENTE_TRASLADO
        
        session.add(cama)
        session.add(paciente)
        session.commit()
        
        # ✅ PASO 4: Remover de la cola de prioridad
        gestor_colas_global.remover_paciente(paciente.id, hospital_id)
        
        # ✅ PASO 5: Notificar cambio
        await notificar_cambio(
            hospital_id=hospital_id,
            evento="asignacion_automatica",
            session=session,
            detalles={
                "paciente_id": paciente.id,
                "paciente_nombre": paciente.nombre,
                "cama_id": cama.id,
                "cama_origen_id": paciente.cama_id if paciente.cama_id else None
            }
        )
        
        print(f"✅ Asignación exitosa: {paciente.nombre} → {cama.id}")
        return True
        
    except Exception as e:
        print(f"❌ Error en realizar_asignacion_automatica: {e}")
        import traceback
        traceback.print_exc()
        session.rollback()
        return False


# ============================================
# LIFESPAN EVENTS
# ============================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, _asignacion_activa
    
    print("🏥 Inicializando base de datos...")
    SQLModel.metadata.create_all(engine)
    print("✅ Base de datos inicializada")
    
    # Inicializar Redis
    if REDIS_AVAILABLE:
        try:
            redis_client = await redis.from_url(
                REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2
            )
            await redis_client.ping()
            print("✅ Conectado a Redis")
        except Exception as e:
            print(f"⚠️ Redis no disponible: {e}")
            redis_client = None
    
    # Sincronizar colas de prioridad con la base de datos
    with Session(engine) as session:
        hospitales = session.exec(select(Hospital)).all()
        for hospital in hospitales:
            gestor_colas_global.sincronizar_cola_con_db(hospital.id, session)
    
    # Iniciar sistema de asignación automática
    _asignacion_activa = True
    tarea_asignacion = asyncio.create_task(proceso_asignacion_automatica())
    
    print("🚀 Servidor listo en http://localhost:8000")
    print("📊 Dashboard disponible en http://localhost:8000/dashboard")
    
    yield
    
    # Detener sistema de asignación automática
    _asignacion_activa = False
    tarea_asignacion.cancel()
    
    if redis_client:
        await redis_client.close()


# ============================================
# CREAR APP
# ============================================

app = FastAPI(
    title="Sistema de Gestión de Camas Hospitalarias",
    description="API con cola de prioridad y asignación automática",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================
# DEPENDENCIAS
# ============================================

def get_session():
    with Session(engine) as session:
        yield session


# ============================================
# WEBSOCKET Y NOTIFICACIONES
# ============================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
    
    async def connect(self, websocket: WebSocket, hospital_id: str):
        await websocket.accept()
        if hospital_id not in self.active_connections:
            self.active_connections[hospital_id] = set()
        self.active_connections[hospital_id].add(websocket)
    
    def disconnect(self, websocket: WebSocket, hospital_id: str):
        if hospital_id in self.active_connections:
            self.active_connections[hospital_id].discard(websocket)
    
    async def broadcast(self, hospital_id: str, message: dict):
        if hospital_id not in self.active_connections:
            return
        
        json_message = json.dumps(message)
        disconnected = set()
        for connection in self.active_connections[hospital_id]:
            try:
                await connection.send_text(json_message)
            except:
                disconnected.add(connection)
        
        for connection in disconnected:
            self.disconnect(connection, hospital_id)


manager = ConnectionManager()


async def enviar_evento(tipo: str, datos: dict):
    """Publica un mensaje a Redis."""
    if not redis_client:
        return
    
    try:
        mensaje = {"tipo": tipo, **datos, "timestamp": datetime.utcnow().isoformat()}
        await redis_client.publish("eventos_camas", json.dumps(mensaje))
    except:
        pass


async def notificar_cambio(
    hospital_id: str,
    evento: str,
    session: Session,
    detalles: Optional[dict] = None
):
    """Notifica cambios tanto via WebSocket como Redis (si está disponible)."""
    
    try:
        # Obtener camas del hospital
        query_camas = select(Cama).where(Cama.hospital_id == hospital_id)
        camas = session.exec(query_camas).all()
        
        # ✅ CORREGIDO: Usar 'en_espera' en lugar de 'en_lista_espera'
        query_pacientes = select(Paciente).where(
            Paciente.hospital_id == hospital_id,
            Paciente.en_espera == True,
            Paciente.cama_id == None
        )
        pacientes_espera = session.exec(query_pacientes).all()
        
        mensaje = {
            "evento": evento,
            "hospital_id": hospital_id,
            "timestamp": datetime.utcnow().isoformat(),
            "camas": [jsonable_encoder(cama) for cama in camas],
            "pacientes_espera": [jsonable_encoder(p) for p in pacientes_espera],
            "detalles": detalles or {}
        }
        
        await manager.broadcast(hospital_id, mensaje)
        await enviar_evento(evento, {"hospital_id": hospital_id, **(detalles or {})})
        
    except Exception as e:
        print(f"⚠️  Error en notificar_cambio: {str(e)}")
        # No lanzar excepción, solo registrar


# ============================================
# SCHEMAS PYDANTIC
# ============================================

from pydantic import BaseModel


class PacienteIngreso(BaseModel):
    nombre: str
    run: str
    sexo: SexoEnum
    edad: int
    enfermedad: EnfermedadEnum
    requerimientos: List[str] = []
    aislamiento: AislamientoEnum = AislamientoEnum.NINGUNO
    es_embarazada: bool = False
    caso_sociosanitario: bool = False
    espera_cardio: bool = False
    diagnostico: Optional[str] = None
    motivo_monitorizacion: Optional[str] = None
    signos_monitorizacion: Optional[str] = None
    notas: Optional[str] = None
    detalle_procedimiento_invasivo: Optional[str] = None


class ReevaluarPacienteRequest(BaseModel):
    enfermedad: Optional[EnfermedadEnum] = None
    requerimientos: List[str] = []
    aislamiento: Optional[AislamientoEnum] = None
    caso_sociosanitario: Optional[bool] = None
    espera_cardio: Optional[bool] = None
    diagnostico: Optional[str] = None
    motivo_monitorizacion: Optional[str] = None
    signos_monitorizacion: Optional[str] = None
    notas: Optional[str] = None
    detalle_procedimiento_invasivo: Optional[str] = None


class DerivarPacienteRequest(BaseModel):
    hospital_destino_id: str
    motivo_derivacion: str


class RechazarDerivacionRequest(BaseModel):
    motivo_rechazo: str


# ============================================
# ENDPOINTS - RAÍZ Y DASHBOARD
# ============================================

@app.get("/", response_class=HTMLResponse)
async def root():
    return """<html><head><meta http-equiv="refresh" content="0; url=/dashboard" /></head></html>"""


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    import os
    # Buscar dashboard.html en el mismo directorio que main.py
    script_dir = os.path.dirname(os.path.abspath(__file__))
    dashboard_path = os.path.join(script_dir, "dashboard.html")
    
    try:
        with open(dashboard_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        # Intentar en directorio actual como fallback
        try:
            with open("dashboard.html", "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        except FileNotFoundError:
            return HTMLResponse(
                content=f"<h1>Dashboard no encontrado</h1><p>Buscado en: {dashboard_path}</p>", 
                status_code=404
            )


# ============================================
# ENDPOINTS - HOSPITALES
# ============================================

@app.get("/hospitales")
def listar_hospitales(session: Session = Depends(get_session)):
    return session.exec(select(Hospital)).all()


@app.get("/hospitales/{hospital_id}")
def obtener_hospital(hospital_id: str, session: Session = Depends(get_session)):
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital {hospital_id} no encontrado")
    return hospital


@app.post("/hospitales/inicializar-multi")
def inicializar_sistema_multihospitalario(session: Session = Depends(get_session)):
    """
    Inicializa el sistema completo con 3 hospitales.
    """
    # Verificar si ya existen hospitales
    query_hospitales = select(Hospital)
    hospitales_existentes = session.exec(query_hospitales).all()
    
    if hospitales_existentes:
        raise HTTPException(
            status_code=400,
            detail="El sistema ya tiene hospitales inicializados. Usa DELETE para limpiar primero."
        )
    
    # Importar la función de models
    from models import init_multihospital_system
    
    # Inicializar los 3 hospitales
    init_multihospital_system(session)
    
    # Obtener estadísticas
    query_hospitales = select(Hospital)
    hospitales = session.exec(query_hospitales).all()
    
    info_hospitales = []
    total_camas = 0
    for hospital in hospitales:
        query_camas = select(Cama).where(Cama.hospital_id == hospital.id)
        num_camas = len(session.exec(query_camas).all())
        total_camas += num_camas
        info_hospitales.append({
            "id": hospital.id,
            "nombre": hospital.nombre,
            "camas": num_camas
        })
    
    # ✅ CORREGIDO: Usar las claves correctas que espera setup_hospital.py
    return {
        "mensaje": "Sistema multi-hospitalario inicializado exitosamente",
        "total_hospitales": len(hospitales),  # ✅ Esta clave es la que faltaba
        "total_camas": total_camas,
        "hospitales": info_hospitales
    }

@app.delete("/hospitales/{hospital_id}")
def eliminar_hospital(hospital_id: str, session: Session = Depends(get_session)):
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail="Hospital no encontrado")
    
    # Eliminar camas y pacientes
    for cama in session.exec(select(Cama).where(Cama.hospital_id == hospital_id)).all():
        session.delete(cama)
    for paciente in session.exec(select(Paciente).where(Paciente.hospital_id == hospital_id)).all():
        session.delete(paciente)
    
    session.delete(hospital)
    session.commit()
    
    # Limpiar cola de prioridad
    gestor_colas_global.obtener_cola(hospital_id).limpiar_cola()
    
    return {"mensaje": f"Hospital {hospital_id} eliminado"}


# ============================================
# ENDPOINTS - ESTADÍSTICAS
# ============================================

@app.get("/hospitales/{hospital_id}/estadisticas")
def obtener_estadisticas(
    hospital_id: str,
    session: Session = Depends(get_session)
):
    """Obtiene estadísticas detalladas de ocupación del hospital."""
    from cola_prioridad import gestor_colas_global
    
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital {hospital_id} no encontrado")
    
    query_camas = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas = session.exec(query_camas).all()
    
    if not todas_camas:
        return {
            "mensaje": "Hospital sin camas configuradas",
            "total_camas": 0,
            "por_estado": {},
            "por_servicio": {},
            "tasa_ocupacion": 0,
            "pacientes_en_espera": 0
        }
    
    estadisticas = {
        "total_camas": len(todas_camas),
        "por_estado": {},
        "por_servicio": {}
    }
    
    for estado in EstadoCamaEnum:
        count = len([c for c in todas_camas if c.estado == estado])
        estadisticas["por_estado"][estado.value] = count
    
    for servicio in ServicioEnum:
        camas_servicio = [c for c in todas_camas if c.servicio == servicio]
        if camas_servicio:
            estadisticas["por_servicio"][servicio.value] = {
                "total": len(camas_servicio),
                "libres": len([c for c in camas_servicio if c.estado == EstadoCamaEnum.LIBRE]),
                "ocupadas": len([c for c in camas_servicio if c.estado == EstadoCamaEnum.OCUPADA]),
                "tasa_ocupacion": round(
                    (len([c for c in camas_servicio if c.estado == EstadoCamaEnum.OCUPADA]) / len(camas_servicio) * 100),
                    2
                )
            }
    
    camas_ocupadas = len([c for c in todas_camas if c.estado == EstadoCamaEnum.OCUPADA])
    estadisticas["tasa_ocupacion"] = round((camas_ocupadas / len(todas_camas)) * 100, 2)
    
    # ✅ CORREGIDO: Usar len() en lugar de tamano()
    try:
        cola = gestor_colas_global.obtener_cola(hospital_id)
        estadisticas["pacientes_en_espera"] = len(cola)
    except Exception as e:
        print(f"⚠️ Error obteniendo cola: {e}")
        # Fallback: contar desde BD
        query_espera = select(Paciente).where(
        Paciente.hospital_id == hospital_id,
        Paciente.en_espera == True,
        Paciente.cama_id == None  # No tienen cama confirmada todavía
    )
        pacientes_espera = session.exec(query_espera).all()
        estadisticas["pacientes_en_espera"] = len(pacientes_espera)
    
    return estadisticas


# ============================================
# ENDPOINTS - CAMAS
# ============================================

@app.get("/hospitales/{hospital_id}/camas")
def listar_camas(hospital_id: str, session: Session = Depends(get_session)):
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital {hospital_id} no encontrado")
    
    query = select(Cama).where(Cama.hospital_id == hospital_id)
    camas = session.exec(query).all()
    
    # Obtener todas las camas para cálculos
    query_todas = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas = session.exec(query_todas).all()
    
    # Crear lista de respuesta con información extendida
    camas_con_info = []
    for cama in camas:
        # Convertir la cama a dict
        cama_dict = cama.model_dump()
        
        if cama.paciente_id:
            paciente = session.get(Paciente, cama.paciente_id)
            if paciente:
                # ✅ CORRECCIÓN: Verificar TODOS los flags persistentes
                requiere_cambio = (
                    paciente.requiere_busqueda_cama or
                    paciente.requiere_nueva_cama or
                    paciente.requiere_cambio_cama or
                    requiere_cambio_cama(paciente, cama, todas_camas)
                )
                
                # Agregar información del paciente al dict
                cama_dict["paciente_info"] = {
                    "id": paciente.id,
                    "nombre": paciente.nombre,
                    "caso_sociosanitario": paciente.caso_sociosanitario,
                    "espera_cardio": paciente.espera_cardio,
                    "requiere_cambio_cama": requiere_cambio,
                    "requiere_busqueda_cama": paciente.requiere_busqueda_cama or paciente.requiere_nueva_cama,
                    "requiere_nueva_cama": paciente.requiere_nueva_cama,
                    "motivo_cambio_cama": paciente.motivo_cambio_cama
                }
                cama_dict["paciente_nombre"] = paciente.nombre
        
        camas_con_info.append(cama_dict)
    
    return camas_con_info


# ============================================
# ENDPOINTS - PACIENTES
# ============================================

@app.get("/hospitales/{hospital_id}/pacientes")
def listar_pacientes(hospital_id: str, session: Session = Depends(get_session)):
    """Lista todos los pacientes del hospital con información actualizada"""
    pacientes = session.exec(select(Paciente).where(Paciente.hospital_id == hospital_id)).all()
    
    resultado = []
    for p in pacientes:
        paciente_dict = {
            "id": p.id,
            "nombre": p.nombre,
            "edad": p.edad,
            "edad_categoria": p.edad_categoria.value,
            "enfermedad": p.enfermedad.value,
            "tiempo_espera_min": p.tiempo_espera_min,
            "en_espera": p.en_espera,
            "cama_id": p.cama_id,
            "cama_destino_id": p.cama_destino_id,
            "es_embarazada": p.es_embarazada,
            "es_adulto_mayor": p.es_adulto_mayor,
            "caso_sociosanitario": p.caso_sociosanitario,
            "espera_cardio": p.espera_cardio,
            "requerimientos": p.requerimientos,
            "aislamiento": p.aislamiento.value,
            "complejidad_requerida": p.complejidad_requerida.value,
            "puntos_complejidad": p.puntos_complejidad,
            "derivacion_pendiente": p.derivacion_pendiente,
            "motivo_derivacion": p.motivo_derivacion,
            "diagnostico": p.diagnostico,
            "notas": p.notas,
            "ingreso": p.fecha_ingreso.isoformat() if p.fecha_ingreso else None,
            # ✅ AGREGADOS: Campos de cambio de cama
            "requiere_cambio_cama": p.requiere_cambio_cama,
            "requiere_busqueda_cama": p.requiere_busqueda_cama,
            "requiere_nueva_cama": p.requiere_nueva_cama,
            "motivo_cambio_cama": p.motivo_cambio_cama
        }
        resultado.append(paciente_dict)
    
    return resultado


@app.get("/hospitales/{hospital_id}/pacientes/{paciente_id}")
def obtener_paciente_detalle(hospital_id: str, paciente_id: str, session: Session = Depends(get_session)):
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece al hospital")
    
    # Información de cama
    cama_info = None
    if paciente.cama_id:
        cama = session.get(Cama, paciente.cama_id)
        if cama:
            cama_info = {
                "id": cama.id,
                "servicio": cama.servicio.value,
                "sala": cama.sala,
                "numero": cama.numero,
                "estado": cama.estado.value
            }
    
    return {
        "id": paciente.id,
        "hospital_id": paciente.hospital_id,
        "nombre": paciente.nombre,
        "run": paciente.run,
        "sexo": paciente.sexo.value,
        "edad": paciente.edad,
        "edad_categoria": paciente.edad_categoria.value,
        "enfermedad": paciente.enfermedad.value,
        "aislamiento": paciente.aislamiento.value,
        "requerimientos": paciente.requerimientos,
        "complejidad_requerida": paciente.complejidad_requerida.value,
        "puntos_complejidad": paciente.puntos_complejidad,
        "es_embarazada": paciente.es_embarazada,
        "caso_sociosanitario": paciente.caso_sociosanitario,
        "espera_cardio": paciente.espera_cardio,
        "diagnostico": paciente.diagnostico,
        "motivo_monitorizacion": paciente.motivo_monitorizacion,
        "signos_monitorizacion": paciente.signos_monitorizacion,
        "notas": paciente.notas,
        "en_lista_espera": paciente.en_lista_espera,
        "tiempo_espera_min": paciente.tiempo_espera_min,
        "cama_id": paciente.cama_id,
        "cama_destino_id": paciente.cama_destino_id,
        "cama_info": cama_info,
        "tipo_paciente": paciente.tipo_paciente.value if paciente.tipo_paciente else "urgencia",
        "prioridad_calculada": calcular_prioridad_paciente(paciente) if paciente.en_lista_espera else 0
    }

# ============================================
# ENDPOINTS - INGRESO DE PACIENTES
# ============================================

@app.post("/hospitales/{hospital_id}/pacientes/ingresar")
async def ingresar_paciente(
    hospital_id: str,
    paciente_data: PacienteIngreso,
    session: Session = Depends(get_session)
):
    """Ingresa un nuevo paciente al hospital y le asigna cama automáticamente si hay disponibles"""
    
    # Validar que el hospital existe
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail=f"Hospital {hospital_id} no encontrado")
    
    # Calcular automáticamente edad_categoria basándose en la edad numérica
    edad_categoria_calculada = determinar_categoria_edad(paciente_data.edad)
    
    # Determinar si es adulto mayor basándose en edad_categoria calculada
    es_adulto_mayor_calculado = (edad_categoria_calculada == EdadCategoriaEnum.ADULTO_MAYOR)
    
    # Crear el paciente con datos calculados
    data_dict = paciente_data.model_dump()
    data_dict['edad_categoria'] = edad_categoria_calculada
    data_dict['es_adulto_mayor'] = es_adulto_mayor_calculado
    
    paciente = Paciente(
        id=f"PAC-{uuid.uuid4().hex[:8].upper()}",
        hospital_id=hospital_id,
        **data_dict,
        en_espera=True,
        tiempo_espera_min=0
    )
    
    # Actualizar complejidad del paciente
    actualizar_complejidad_paciente(paciente)
    
    session.add(paciente)
    session.commit()
    session.refresh(paciente)
    
    print(f"✅ Paciente ingresado: {paciente.nombre} (ID: {paciente.id})")
    print(f"   Complejidad: {paciente.complejidad_requerida.value} ({paciente.puntos_complejidad} puntos)")
    
    # ✅ ASIGNACIÓN AUTOMÁTICA DE CAMA
    cama_asignada = None
    mensaje_cama = ""
    
    try:
        # Buscar camas disponibles
        query_camas = select(Cama).where(
            Cama.hospital_id == hospital_id,
            Cama.estado == EstadoCamaEnum.LIBRE
        )
        camas_disponibles = session.exec(query_camas).all()
        
        if not camas_disponibles:
            print(f"⚠️  No hay camas disponibles para {paciente.nombre}")
            mensaje_cama = "sin camas disponibles - En lista de espera"
        else:
            # Obtener TODAS las camas del hospital
            query_todas_camas = select(Cama).where(Cama.hospital_id == hospital_id)
            todas_camas_hospital = session.exec(query_todas_camas).all()
            
            # Intentar asignar cama
            cama_asignada = asignar_cama(
                paciente=paciente,
                camas_disponibles=camas_disponibles,
                todas_camas=todas_camas_hospital,
                session=session
            )
            
            if cama_asignada:
                # Actualizar sexo de sala si es compartida
                actualizar_sexo_sala(cama_asignada, paciente, todas_camas_hospital, session)
                
                # Reservar la cama en estado PENDIENTE_TRASLADO (amarillo)
                cama_asignada.estado = EstadoCamaEnum.PENDIENTE_TRASLADO
                cama_asignada.paciente_id = paciente.id
                paciente.cama_destino_id = cama_asignada.id
                
                session.add(cama_asignada)
                session.add(paciente)
                session.commit()
                session.refresh(paciente)
                session.refresh(cama_asignada)
                
                print(f"✅ Cama reservada: {paciente.nombre} → {cama_asignada.id}")
                mensaje_cama = f"Cama {cama_asignada.id} asignada (pendiente confirmación)"
            else:
                print(f"⚠️  No hay cama compatible para {paciente.nombre}")
                mensaje_cama = "sin cama compatible - En lista de espera"
    
    except Exception as e:
        print(f"❌ Error al asignar cama: {str(e)}")
        mensaje_cama = "Error en asignación - En lista de espera"
        # Continuar sin cama, el paciente queda en espera
    
    # Notificar cambios
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="paciente_ingresado",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "cama_asignada": cama_asignada.id if cama_asignada else None,
            "complejidad": paciente.complejidad_requerida.value
        }
    )
    
    return {
        "mensaje": f"Paciente {paciente.nombre} registrado - {mensaje_cama}",
        "paciente": jsonable_encoder(paciente),
        "cama_asignada": jsonable_encoder(cama_asignada) if cama_asignada else None,
        "complejidad": paciente.complejidad_requerida.value,
        "puntos": paciente.puntos_complejidad
    }

# ============================================
# ENDPOINTS - REEVALUACIÓN DE PACIENTES
# ============================================

@app.put("/hospitales/{hospital_id}/pacientes/{paciente_id}/reevaluar")
async def reevaluar_paciente_completo(
    hospital_id: str,
    paciente_id: str,
    request_data: ReevaluarPacienteRequest,
    session: Session = Depends(get_session)
):
    """
    Reevaluación completa del paciente.
    ✅ CORREGIDO: No agrega automáticamente a la cola, solo marca que requiere cambio.
    """
    from cola_prioridad import gestor_colas_global
    
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece al hospital")
    
    # Guardar valores anteriores
    enfermedad_anterior = paciente.enfermedad
    aislamiento_anterior = paciente.aislamiento
    
    # Actualizar campos
    if request_data.enfermedad is not None:
        paciente.enfermedad = request_data.enfermedad
    
    paciente.requerimientos = request_data.requerimientos
    
    if request_data.aislamiento is not None:
        paciente.aislamiento = request_data.aislamiento
    
    if request_data.caso_sociosanitario is not None:
        paciente.caso_sociosanitario = request_data.caso_sociosanitario
    if request_data.espera_cardio is not None:
        paciente.espera_cardio = request_data.espera_cardio
    
    if request_data.diagnostico is not None:
        paciente.diagnostico = request_data.diagnostico
    if request_data.notas is not None:
        paciente.notas = request_data.notas
    
    # Actualizar complejidad
    actualizar_complejidad_paciente(paciente)
    
    session.add(paciente)
    
    # ✅ PRIMERO: Verificar si ya está en cola y removerlo
    gestor_cola = gestor_colas_global.obtener_cola(hospital_id)
    if gestor_cola.esta_en_cola(paciente.id):
        gestor_cola.remover_paciente(paciente.id)
        paciente.en_lista_espera = False
        print(f"🔄 Paciente {paciente.id} removido de cola para re-evaluar")
    
    # ✅ LIMPIAR CAMA DESTINO SI TIENE (cancelar asignación anterior)
    if paciente.cama_destino_id:
        cama_destino_anterior = session.get(Cama, paciente.cama_destino_id)
        if cama_destino_anterior and cama_destino_anterior.estado == EstadoCamaEnum.PENDIENTE_TRASLADO:
            cama_destino_anterior.estado = EstadoCamaEnum.LIBRE
            cama_destino_anterior.paciente_id = None
            session.add(cama_destino_anterior)
            print(f"🔄 Cama destino anterior {cama_destino_anterior.id} liberada")
        paciente.cama_destino_id = None
    
    # ✅ Restaurar cama origen a OCUPADA si estaba EN_TRASLADO
    if paciente.cama_id:
        cama_origen = session.get(Cama, paciente.cama_id)
        if cama_origen and cama_origen.estado == EstadoCamaEnum.EN_TRASLADO:
            cama_origen.estado = EstadoCamaEnum.OCUPADA
            session.add(cama_origen)
    
    # Verificar si requiere alta
    if requiere_alta(paciente):
        cama_actual = session.get(Cama, paciente.cama_id) if paciente.cama_id else None
        if cama_actual and cama_actual.estado == EstadoCamaEnum.OCUPADA:
            cama_actual.estado = EstadoCamaEnum.ALTA_SUGERIDA
            session.add(cama_actual)
            session.commit()
            
            return {
                "mensaje": "Alta sugerida automáticamente",
                "paciente_id": paciente.id,
                "requiere_alta": True,
                "requiere_cambio_cama": False
            }
    
# Verificar si requiere cambio de cama
    cama_actual = session.get(Cama, paciente.cama_id) if paciente.cama_id else None
    
    if cama_actual and requiere_cambio_cama(paciente, cama_actual):
        # ✅ CORRECCIÓN: Marcar TODOS los flags necesarios
        # ✅ LIMPIAR FLAGS DE CAMBIO DE CAMA
        paciente.requiere_nueva_cama = False
        paciente.requiere_busqueda_cama = False
        paciente.requiere_cambio_cama = False
        paciente.motivo_cambio_cama = None
        
        # Calcular motivo del cambio para mostrar en UI
        servicio_requerido = determinar_servicio_requerido(paciente)
        motivo = f"Requerimientos actualizados. Servicio requerido: {servicio_requerido.value if servicio_requerido else 'ninguno'}"
        paciente.motivo_cambio_cama = motivo
        
        # ✅ CORRECCIÓN: Actualizar estado de la cama a REQUIERE_BUSQUEDA_CAMA
        cama_actual.estado = EstadoCamaEnum.REQUIERE_BUSQUEDA_CAMA
        session.add(cama_actual)
        
        session.add(paciente)
        session.commit()
        session.refresh(paciente)
        session.refresh(cama_actual)
        
        await notificar_cambio(
            hospital_id=hospital_id,
            evento="paciente_requiere_cambio_cama",
            session=session,
            detalles={
                "paciente_id": paciente.id,
                "paciente_nombre": paciente.nombre,
                "cama_id": cama_actual.id,
                "motivo": motivo
            }
        )
        
        return {
            "mensaje": "Paciente reevaluado. Requiere cambio de cama - Presione 'Asignar Nueva Cama'",
            "paciente_id": paciente.id,
            "requiere_cambio_cama": True,
            "requiere_busqueda_cama": True,
            "requiere_alta": False,
            "motivo_cambio": motivo
        }
    
    # No requiere cambios
    paciente.requiere_nueva_cama = False
    session.commit()
    
    return {
        "mensaje": "Paciente reevaluado exitosamente",
        "paciente_id": paciente.id,
        "requiere_cambio_cama": False,
        "requiere_alta": False
    }


@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/agregar-a-lista-espera")
async def agregar_paciente_a_lista_espera(
    hospital_id: str,
    paciente_id: str,
    session: Session = Depends(get_session)
):
    """
    Agrega manualmente un paciente a la lista de espera para asignación automática.
    ✅ CORREGIDO: Limpia flags de cambio de cama y maneja estados correctamente.
    """
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece al hospital")
    
    # ✅ VALIDACIÓN: Si ya tiene cama destino, limpiarla primero
    if paciente.cama_destino_id:
        cama_destino = session.get(Cama, paciente.cama_destino_id)
        if cama_destino and cama_destino.estado == EstadoCamaEnum.PENDIENTE_TRASLADO:
            cama_destino.estado = EstadoCamaEnum.LIBRE
            cama_destino.paciente_id = None
            session.add(cama_destino)
            print(f"🧹 Limpiando cama destino previa: {cama_destino.id}")
        
        paciente.cama_destino_id = None
    
    # ✅ CORRECCIÓN: Manejar estado de cama actual
    if paciente.cama_id:
        cama_actual = session.get(Cama, paciente.cama_id)
        if cama_actual:
            # Si está en REQUIERE_BUSQUEDA_CAMA o EN_TRASLADO, volver a OCUPADA
            if cama_actual.estado in [EstadoCamaEnum.REQUIERE_BUSQUEDA_CAMA, EstadoCamaEnum.EN_TRASLADO]:
                cama_actual.estado = EstadoCamaEnum.OCUPADA
                session.add(cama_actual)
                print(f"🔄 Cama {cama_actual.id} vuelta a OCUPADA")
    
    # ✅ CORRECCIÓN: Limpiar flags de cambio de cama (ya está en proceso)
    paciente.requiere_nueva_cama = False
    paciente.requiere_busqueda_cama = False
    paciente.requiere_cambio_cama = False
    # Mantener motivo_cambio_cama para referencia
    
    # Marcar como en lista de espera
    paciente.en_lista_espera = True
    paciente.en_espera = True
    paciente.tipo_paciente = TipoPacienteEnum.HOSPITALIZADO if paciente.cama_id else TipoPacienteEnum.URGENCIA
    
    session.add(paciente)
    session.commit()
    session.refresh(paciente)
    
    # Agregar a la cola (si ya estaba, actualiza prioridad)
    cola = gestor_colas_global.obtener_cola(hospital_id)
    if cola.esta_en_cola(paciente.id):
        print(f"⚠️ Paciente {paciente.nombre} ya está en cola - actualizando")
        cola.actualizar_prioridad(paciente, session)
    else:
        gestor_colas_global.agregar_paciente(paciente, hospital_id, session)
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="paciente_agregado_lista_espera",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "requiere_cambio_cama": True
        }
    )
    
    print(f"✅ Paciente {paciente.nombre} agregado a cola de prioridad para asignación automática")
    
    return {
        "mensaje": f"Paciente {paciente.nombre} agregado a lista de espera para asignación automática",
        "paciente_id": paciente.id,
        "en_cola": True
    }

# ============================================
# ENDPOINTS - CONFIRMACIÓN DE TRASLADOS
# ============================================

@app.post("/hospitales/{hospital_id}/camas/{cama_id}/completar-traslado")
async def completar_traslado(
    hospital_id: str,
    cama_id: str,
    session: Session = Depends(get_session)
):
    """
    CORREGIDO: Completa el traslado confirmando que el paciente llego a la cama destino.
    Automaticamente egresa al paciente de la cama origen si esta pendiente.
    
    Flujo:
    1. Cama destino esta en PENDIENTE_TRASLADO
    2. Se confirma que el paciente llego
    3. Paciente se asigna a esta cama (OCUPADA)
    4. Si hay cama origen, se libera automaticamente
    """
    cama_destino = session.get(Cama, cama_id)
    
    if not cama_destino:
        raise HTTPException(status_code=404, detail="Cama no encontrada")
    
    if cama_destino.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Cama no pertenece al hospital")
    
    if cama_destino.estado != EstadoCamaEnum.PENDIENTE_TRASLADO:
        raise HTTPException(
            status_code=400, 
            detail=f"Solo se puede completar traslado en cama PENDIENTE_TRASLADO. Estado actual: {cama_destino.estado}"
        )
    
    paciente_id = cama_destino.paciente_id
    if not paciente_id:
        raise HTTPException(status_code=400, detail="No hay paciente asignado a esta cama")
    
    paciente = session.get(Paciente, paciente_id)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    # Obtener cama origen (si existe)
    cama_origen = None
    hospital_origen_id = paciente.hospital_origen_id
    
    if paciente.cama_id:
        cama_origen = session.get(Cama, paciente.cama_id)
    
    # PASO 1: Liberar cama origen AUTOMATICAMENTE
    if cama_origen:
        if cama_origen.estado == EstadoCamaEnum.EN_TRASLADO:
            # Liberar cama origen y actualizar sexo de sala si es compartida
            cama_origen.estado = EstadoCamaEnum.LIBRE
            cama_origen.paciente_id = None
            
            # Obtener todas las camas para actualizar sexo de sala
            query_todas = select(Cama).where(Cama.hospital_id == cama_origen.hospital_id)
            todas_camas = session.exec(query_todas).all()
            
            # Liberar sexo de sala si es compartida
            liberar_sexo_sala(cama_origen, todas_camas, session)
            
            session.add(cama_origen)
            print(f"OK: Cama origen {cama_origen.id} liberada automaticamente")
    
    # PASO 2: Asignar paciente a cama destino
    cama_destino.estado = EstadoCamaEnum.OCUPADA
    cama_destino.paciente_id = paciente.id
    paciente.cama_id = cama_destino.id
    paciente.en_espera = False
    
    # NUEVO: Limpiar flags de derivacion una vez completado el traslado
    paciente.paciente_aceptado_destino = False
    paciente.timestamp_aceptacion_derivacion = None
    
    # Actualizar sexo de sala si es compartida
    query_todas_destino = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas_destino = session.exec(query_todas_destino).all()
    actualizar_sexo_sala(cama_destino, paciente, todas_camas_destino, session)
    
    session.add(cama_destino)
    session.add(paciente)
    session.commit()
    session.refresh(cama_destino)
    session.refresh(paciente)
    
    # NOTIFICAR al hospital origen que el paciente ya fue egresado automaticamente
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="traslado_completado",
        session=session,
        detalles={
            "cama_origen_id": cama_origen.id if cama_origen else None,
            "cama_destino_id": cama_destino.id,
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "hospital_origen": hospital_origen_id
        }
    )
    
    # Notificar tambien al hospital origen si existe
    if hospital_origen_id:
        await notificar_cambio(
            hospital_id=hospital_origen_id,
            evento="paciente_egresado_automatico",
            session=session,
            detalles={
                "cama_origen_id": cama_origen.id if cama_origen else None,
                "paciente_id": paciente.id,
                "paciente_nombre": paciente.nombre,
                "hospital_destino": hospital_id,
                "cama_destino": cama_destino.id
            }
        )
    
    print(f"OK: Traslado completado: Paciente {paciente.nombre} movido a cama {cama_destino.id}")
    print(f"OK: Cama origen {cama_origen.id if cama_origen else 'N/A'} liberada automaticamente")
    
    return {
        "mensaje": f"Traslado completado. Paciente asignado a {cama_destino.id}",
        "cama_destino_id": cama_destino.id,
        "cama_origen_id": cama_origen.id if cama_origen else None,
        "cama_origen_liberada": True,  # NUEVO
        "paciente": {
            "id": paciente.id,
            "nombre": paciente.nombre
        }
    }

@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/cancelar-traslado-derivacion")
async def cancelar_traslado_derivacion(
    hospital_id: str,
    paciente_id: str,
    session: Session = Depends(get_session)
):
    """
    NUEVO: Cancela el traslado de un paciente derivado cuando esta en estado "paciente aceptado".
    El paciente vuelve a su cama original en el hospital de origen.
    Solo puede ejecutarse desde el hospital ORIGEN.
    """
    # Verificar que el hospital actual es el hospital origen
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if not paciente.hospital_origen_id:
        raise HTTPException(status_code=400, detail="Este paciente no tiene derivacion pendiente")
    
    if hospital_id != paciente.hospital_origen_id:
        raise HTTPException(
            status_code=400, 
            detail="Solo el hospital de origen puede cancelar el traslado"
        )
    
    if not paciente.paciente_aceptado_destino:
        raise HTTPException(
            status_code=400,
            detail="El paciente no esta en estado 'aceptado en destino'"
        )
    
    # Obtener la cama origen
    cama_origen = session.get(Cama, paciente.cama_id) if paciente.cama_id else None
    
    if not cama_origen:
        raise HTTPException(status_code=400, detail="No hay cama de origen registrada")
    
    if cama_origen.estado != EstadoCamaEnum.EN_TRASLADO:
        raise HTTPException(
            status_code=400,
            detail=f"La cama de origen debe estar en estado EN_TRASLADO. Estado actual: {cama_origen.estado}"
        )
    
    # Obtener la cama destino (la que estaba pendiente)
    cama_destino = session.get(Cama, paciente.cama_destino_id) if paciente.cama_destino_id else None
    
    # PASO 1: Liberar cama destino (la del hospital de destino)
    if cama_destino:
        cama_destino.estado = EstadoCamaEnum.LIBRE
        cama_destino.paciente_id = None
        session.add(cama_destino)
        print(f"OK: Cama destino {cama_destino.id} liberada")
    
    # PASO 2: Restaurar cama origen a OCUPADA
    cama_origen.estado = EstadoCamaEnum.OCUPADA
    cama_origen.paciente_id = paciente.id
    session.add(cama_origen)
    
    # PASO 3: Limpiar datos de derivacion del paciente
    paciente.hospital_origen_id = None
    paciente.cama_destino_id = None
    paciente.paciente_aceptado_destino = False
    paciente.timestamp_aceptacion_derivacion = None
    paciente.derivacion_pendiente = False
    paciente.en_espera = False
    paciente.motivo_derivacion = None
    paciente.motivo_rechazo_derivacion = None
    
    session.add(paciente)
    session.commit()
    session.refresh(cama_origen)
    session.refresh(paciente)
    
    # Notificar ambos hospitales
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="traslado_cancelado_origen",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "cama_vuelve_a": cama_origen.id
        }
    )
    
    if cama_destino:
        await notificar_cambio(
            hospital_id=cama_destino.hospital_id,
            evento="traslado_cancelado_destino",
            session=session,
            detalles={
                "paciente_id": paciente.id,
                "paciente_nombre": paciente.nombre,
                "cama_liberada": cama_destino.id
            }
        )
    
    print(f"OK: Traslado cancelado: {paciente.nombre} vuelve a {cama_origen.id}")
    
    return {
        "mensaje": f"Traslado cancelado. Paciente {paciente.nombre} restaurado a cama {cama_origen.id}",
        "paciente_id": paciente.id,
        "cama_restaurada": cama_origen.id,
        "cama_destino_liberada": cama_destino.id if cama_destino else None
    }

@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/egresar-paciente-derivacion")
async def egresar_paciente_derivacion(
    hospital_id: str,
    paciente_id: str,
    session: Session = Depends(get_session)
):
    """
    NUEVO: Egresa un paciente del hospital de origen cuyo traslado ya fue aceptado.
    Solo puede ejecutarse desde el hospital ORIGEN cuando el paciente esta en estado "paciente aceptado".
    Libera la cama origen inmediatamente (sin esperar confirmacion en destino).
    """
    # Verificar que el hospital actual es el hospital origen
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if not paciente.hospital_origen_id:
        raise HTTPException(status_code=400, detail="Este paciente no tiene derivacion pendiente")
    
    if hospital_id != paciente.hospital_origen_id:
        raise HTTPException(
            status_code=400, 
            detail="Solo el hospital de origen puede egresar al paciente"
        )
    
    if not paciente.paciente_aceptado_destino:
        raise HTTPException(
            status_code=400,
            detail="El paciente debe estar en estado 'aceptado en destino' para egresar"
        )
    
    # Obtener la cama origen
    cama_origen = session.get(Cama, paciente.cama_id) if paciente.cama_id else None
    
    if not cama_origen:
        raise HTTPException(status_code=400, detail="No hay cama de origen registrada")
    
    if cama_origen.estado != EstadoCamaEnum.EN_TRASLADO:
        raise HTTPException(
            status_code=400,
            detail=f"La cama de origen debe estar en estado EN_TRASLADO. Estado actual: {cama_origen.estado}"
        )
    
    # PASO 1: Liberar la cama origen
    from logic import liberar_sexo_sala
    
    # Obtener todas las camas para actualizar sexo de sala
    query_todas = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas = session.exec(query_todas).all()
    
    # Liberar sexo de sala si es compartida
    liberar_sexo_sala(cama_origen, todas_camas, session)
    
    cama_origen.estado = EstadoCamaEnum.LIBRE
    cama_origen.paciente_id = None
    session.add(cama_origen)
    
    # PASO 2: Actualizar paciente
    # El paciente ya no esta en el hospital origen, pero mantener los datos de derivacion
    # hasta que sea confirmado en destino
    paciente.cama_id = None
    
    session.add(paciente)
    session.commit()
    session.refresh(cama_origen)
    session.refresh(paciente)
    
    # Notificar ambos hospitales
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="paciente_egresado_derivacion",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "cama_liberada": cama_origen.id
        }
    )
    
    if paciente.cama_destino_id:
        cama_destino = session.get(Cama, paciente.cama_destino_id)
        if cama_destino:
            await notificar_cambio(
                hospital_id=cama_destino.hospital_id,
                evento="paciente_egresado_hospital_origen",
                session=session,
                detalles={
                    "paciente_id": paciente.id,
                    "paciente_nombre": paciente.nombre,
                    "cama_destino": cama_destino.id
                }
            )
    
    print(f"OK: Paciente egresado: {paciente.nombre} - Cama {cama_origen.id} liberada")
    
    return {
        "mensaje": f"Paciente {paciente.nombre} egresado. Cama {cama_origen.id} liberada",
        "paciente_id": paciente.id,
        "cama_liberada": cama_origen.id,
        "paciente_en_transito_a_destino": True
    }

@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/confirmar-traslado")
async def confirmar_traslado_paciente(
    hospital_id: str,
    paciente_id: str,
    session: Session = Depends(get_session)
):
    """
    Confirma el traslado de un paciente a su cama destino asignada.
    Alternativa al endpoint por cama_id, útil cuando se tiene el ID del paciente.
    """
    from cola_prioridad import gestor_colas_global
    from logic import liberar_sexo_sala, actualizar_sexo_sala
    
    # Buscar el paciente
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece al hospital")
    
    # Verificar que tiene cama destino asignada
    if not paciente.cama_destino_id:
        raise HTTPException(
            status_code=400, 
            detail="El paciente no tiene cama destino asignada"
        )
    
    # Obtener la cama destino
    cama_destino = session.get(Cama, paciente.cama_destino_id)
    
    if not cama_destino:
        raise HTTPException(status_code=404, detail="Cama destino no encontrada")
    
    if cama_destino.estado != EstadoCamaEnum.PENDIENTE_TRASLADO:
        raise HTTPException(
            status_code=400, 
            detail=f"La cama destino no está en estado PENDIENTE_TRASLADO. Estado actual: {cama_destino.estado}"
        )
    
    # Obtener cama origen (si existe)
    cama_origen = None
    if paciente.cama_id and paciente.cama_id != cama_destino.id:
        cama_origen = session.get(Cama, paciente.cama_id)
    
    # Obtener todas las camas
    query_todas = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas = session.exec(query_todas).all()
    
    # Liberar cama origen
    if cama_origen:
        liberar_sexo_sala(cama_origen, todas_camas, session)
        cama_origen.estado = EstadoCamaEnum.LIBRE
        cama_origen.paciente_id = None
        session.add(cama_origen)
        print(f"✅ Cama origen {cama_origen.id} liberada")
    
    # Ocupar cama destino
    actualizar_sexo_sala(cama_destino, paciente, todas_camas, session)
    cama_destino.estado = EstadoCamaEnum.OCUPADA
    cama_destino.paciente_id = paciente.id
    session.add(cama_destino)
    
    # Actualizar paciente - limpiar todos los flags
    paciente.cama_id = cama_destino.id
    paciente.cama_destino_id = None
    paciente.en_espera = False
    paciente.en_lista_espera = False
    paciente.requiere_nueva_cama = False
    paciente.requiere_cambio_cama = False
    paciente.prioridad_calculada = 0
    session.add(paciente)
    
    # Asegurar que no esté en la cola
    gestor_cola = gestor_colas_global.obtener_cola(hospital_id)
    if gestor_cola.esta_en_cola(paciente.id):
        gestor_cola.remover_paciente(paciente.id, session, paciente)
        print(f"✅ Paciente {paciente.id} removido de cola de prioridad")
    
    session.commit()
    session.refresh(cama_destino)
    session.refresh(paciente)
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="traslado_completado",
        session=session,
        detalles={
            "cama_origen_id": cama_origen.id if cama_origen else None,
            "cama_destino_id": cama_destino.id,
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre
        }
    )
    
    print(f"✅ Traslado confirmado: {paciente.nombre} ahora en {cama_destino.id}")
    
    return {
        "mensaje": f"Traslado confirmado. Paciente en cama {cama_destino.id}",
        "cama_destino_id": cama_destino.id,
        "cama_origen_id": cama_origen.id if cama_origen else None,
        "paciente": {
            "id": paciente.id,
            "nombre": paciente.nombre
        }
    }

@app.post("/hospitales/{hospital_id}/camas/{cama_id}/rechazar-traslado")
async def rechazar_traslado(
    hospital_id: str,
    cama_id: str,
    session: Session = Depends(get_session)
):
    """
    Rechaza un traslado pendiente. El paciente vuelve a la cola de prioridad.
    ✅ NUEVO: Endpoint para rechazar asignación automática.
    """
    from cola_prioridad import gestor_colas_global
    
    cama = session.get(Cama, cama_id)
    
    if not cama:
        raise HTTPException(status_code=404, detail="Cama no encontrada")
    
    if cama.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Cama no pertenece al hospital")
    
    if cama.estado != EstadoCamaEnum.PENDIENTE_TRASLADO:
        raise HTTPException(status_code=400, detail="Solo se puede rechazar traslados pendientes")
    
    paciente_id = cama.paciente_id
    if not paciente_id:
        raise HTTPException(status_code=400, detail="No hay paciente en esta cama")
    
    paciente = session.get(Paciente, paciente_id)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    # Liberar la cama
    cama.estado = EstadoCamaEnum.LIBRE
    cama.paciente_id = None
    session.add(cama)
    
    # Si tiene cama origen, restaurarla a OCUPADA
    if paciente.cama_id:
        cama_origen = session.get(Cama, paciente.cama_id)
        if cama_origen and cama_origen.estado == EstadoCamaEnum.EN_TRASLADO:
            cama_origen.estado = EstadoCamaEnum.OCUPADA
            session.add(cama_origen)
    
    # Limpiar cama destino del paciente
    paciente.cama_destino_id = None
    
    # Re-agregar a la cola de prioridad
    paciente.tipo_paciente = TipoPacienteEnum.HOSPITALIZADO if paciente.cama_id else TipoPacienteEnum.URGENCIA
    paciente.en_lista_espera = True
    paciente.requiere_nueva_cama = True
    session.add(paciente)
    session.commit()
    
    # Agregar a cola
    gestor_cola = gestor_colas_global.obtener_cola(hospital_id)
    prioridad = gestor_cola.agregar_paciente(paciente, session)
    session.commit()
    
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="traslado_rechazado",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "cama_rechazada": cama_id
        }
    )
    
    return {
        "mensaje": "Traslado rechazado. Paciente vuelve a lista de espera.",
        "paciente_id": paciente.id,
        "prioridad": prioridad
    }


# ============================================
# ENDPOINTS - ALTAS
# ============================================

@app.post("/hospitales/{hospital_id}/camas/{cama_id}/sugerir-alta")
async def sugerir_alta(hospital_id: str, cama_id: str, session: Session = Depends(get_session)):
    """Marca una cama como candidata para alta."""
    cama = session.get(Cama, cama_id)
    
    if not cama or cama.hospital_id != hospital_id:
        raise HTTPException(status_code=404, detail="Cama no encontrada")
    
    if cama.estado != EstadoCamaEnum.OCUPADA:
        raise HTTPException(status_code=400, detail="Solo se puede sugerir alta en camas ocupadas")
    
    cama.estado = EstadoCamaEnum.ALTA_SUGERIDA
    session.add(cama)
    session.commit()
    
    await notificar_cambio(hospital_id, "alta_sugerida", session, {"cama_id": cama.id})
    
    return {"mensaje": "Alta sugerida", "cama_id": cama.id}


@app.post("/hospitales/{hospital_id}/camas/{cama_id}/alta")
async def confirmar_alta(hospital_id: str, cama_id: str, session: Session = Depends(get_session)):
    """Confirma el alta y libera la cama."""
    cama = session.get(Cama, cama_id)
    
    if not cama or cama.hospital_id != hospital_id:
        raise HTTPException(status_code=404, detail="Cama no encontrada")
    
    if not cama.paciente_id:
        raise HTTPException(status_code=400, detail="No hay paciente en esta cama")
    
    paciente = session.get(Paciente, cama.paciente_id)
    paciente_nombre = paciente.nombre if paciente else "Desconocido"
    
    todas_camas = session.exec(select(Cama).where(Cama.hospital_id == hospital_id)).all()
    liberar_sexo_sala(cama, todas_camas, session)
    
    cama.estado = EstadoCamaEnum.LIBRE
    cama.paciente_id = None
    
    if paciente:
        paciente.cama_id = None
        paciente.en_lista_espera = False
        paciente.en_espera = False
        session.add(paciente)
    
    session.add(cama)
    session.commit()
    
    await notificar_cambio(hospital_id, "alta_confirmada", session, {"cama_id": cama.id, "paciente_nombre": paciente_nombre})
    
    return {"mensaje": f"Alta confirmada para {paciente_nombre}", "cama_id": cama.id}


@app.post("/hospitales/{hospital_id}/camas/{cama_id}/cancelar-alta")
async def cancelar_alta(hospital_id: str, cama_id: str, session: Session = Depends(get_session)):
    """Cancela una alta sugerida."""
    cama = session.get(Cama, cama_id)
    
    if not cama or cama.hospital_id != hospital_id:
        raise HTTPException(status_code=404, detail="Cama no encontrada")
    
    if cama.estado != EstadoCamaEnum.ALTA_SUGERIDA:
        raise HTTPException(status_code=400, detail="La cama no tiene alta sugerida")
    
    cama.estado = EstadoCamaEnum.OCUPADA
    session.add(cama)
    session.commit()
    
    await notificar_cambio(hospital_id, "alta_cancelada", session, {"cama_id": cama.id})
    
    return {"mensaje": "Alta cancelada", "cama_id": cama.id}

    # ============================================
# ENDPOINTS - DERIVACIÓN ENTRE HOSPITALES
# ============================================

@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/derivar")
async def derivar_paciente(
    hospital_id: str,
    paciente_id: str,
    derivacion_data: DerivarPacienteRequest,
    session: Session = Depends(get_session)
):
    """
    Deriva un paciente a otro hospital.
    El paciente queda en lista de espera del hospital destino hasta ser aceptado.
    """
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece al hospital")
    
    hospital_destino = session.get(Hospital, derivacion_data.hospital_destino_id)
    if not hospital_destino:
        raise HTTPException(status_code=404, detail="Hospital destino no encontrado")
    
    if derivacion_data.hospital_destino_id == hospital_id:
        raise HTTPException(status_code=400, detail="No se puede derivar al mismo hospital")
    
    # Guardar información de origen
    paciente.hospital_origen_id = hospital_id
    paciente.motivo_derivacion = derivacion_data.motivo_derivacion
    paciente.derivacion_pendiente = True
    
    # Si tiene cama, marcarla EN_TRASLADO
    if paciente.cama_id:
        cama_origen = session.get(Cama, paciente.cama_id)
        if cama_origen:
            paciente.cama_origen_id = paciente.cama_id
            cama_origen.estado = EstadoCamaEnum.EN_TRASLADO
            session.add(cama_origen)
    
    # Cambiar al hospital destino
    paciente.hospital_id = derivacion_data.hospital_destino_id
    paciente.cama_id = None
    paciente.cama_destino_id = None
    paciente.en_lista_espera = True
    paciente.en_espera = True
    paciente.tipo_paciente = TipoPacienteEnum.DERIVADO
    
    # Eliminar de cola del hospital origen
    gestor_colas_global.eliminar_paciente(paciente.id, hospital_id)
    
    session.add(paciente)
    session.commit()
    
    print(f"✅ Paciente {paciente.nombre} derivado de {hospital_id} a {derivacion_data.hospital_destino_id}")
    
    # Notificar ambos hospitales
    await notificar_cambio(hospital_id, "paciente_derivado_salida", session, {
        "paciente_id": paciente.id,
        "paciente_nombre": paciente.nombre,
        "hospital_destino": hospital_destino.nombre
    })
    
    await notificar_cambio(derivacion_data.hospital_destino_id, "paciente_derivado_entrada", session, {
        "paciente_id": paciente.id,
        "paciente_nombre": paciente.nombre,
        "hospital_origen": hospital_id
    })
    
    return {
        "mensaje": f"Paciente derivado a {hospital_destino.nombre}",
        "paciente_id": paciente.id,
        "hospital_destino": hospital_destino.nombre,
        "pendiente_aceptacion": True
    }


@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/aceptar-derivacion")
async def aceptar_derivacion(
    hospital_id: str,
    paciente_id: str,
    session: Session = Depends(get_session)
):
    """
    Acepta un paciente derivado y le asigna cama automaticamente.
    Marca el paciente como aceptado pero no egresado de su hospital origen.
    """
    from logic import asignar_cama
    
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece a este hospital")
    
    if not paciente.derivacion_pendiente:
        raise HTTPException(status_code=400, detail="El paciente no tiene derivacion pendiente")
    
    # Buscar cama disponible automaticamente
    query_camas = select(Cama).where(
        Cama.hospital_id == hospital_id,
        Cama.estado == EstadoCamaEnum.LIBRE
    )
    camas_disponibles = session.exec(query_camas).all()
    
    if not camas_disponibles:
        raise HTTPException(
            status_code=400, 
            detail="No hay camas disponibles. Rechaza la derivacion o espera a que se libere una cama."
        )
    
    # Obtener TODAS las camas del hospital para la logica de asignacion
    query_todas_camas = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas = session.exec(query_todas_camas).all()
    
    # Asignar cama automaticamente
    cama_asignada = asignar_cama(paciente, list(camas_disponibles), todas_camas, session)
    
    if not cama_asignada:
        raise HTTPException(
            status_code=400,
            detail="No se encontro cama adecuada para los requerimientos del paciente"
        )
    
    # CAMBIO: Marcar como aceptado pero pendiente de egreso en hospital origen
    cama_asignada.estado = EstadoCamaEnum.PENDIENTE_TRASLADO
    cama_asignada.paciente_id = paciente.id
    paciente.cama_destino_id = cama_asignada.id
    paciente.derivacion_pendiente = False
    paciente.paciente_aceptado_destino = True  # NUEVO
    paciente.timestamp_aceptacion_derivacion = datetime.utcnow()  # NUEVO
    
    # Actualizar sexo de sala en la nueva cama
    from logic import actualizar_sexo_sala
    query_todas_destino = select(Cama).where(Cama.hospital_id == hospital_id)
    todas_camas_destino = session.exec(query_todas_destino).all()
    actualizar_sexo_sala(cama_asignada, paciente, todas_camas_destino, session)
    
    session.add(cama_asignada)
    session.add(paciente)
    session.commit()
    session.refresh(paciente)
    session.refresh(cama_asignada)
    
    # Notificar ambos hospitales
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="derivacion_aceptada",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "cama_asignada": cama_asignada.id
        }
    )
    
    if paciente.hospital_origen_id:
        await notificar_cambio(
            hospital_id=paciente.hospital_origen_id,
            evento="derivacion_aceptada_notificacion",
            session=session,
            detalles={
                "paciente_id": paciente.id,
                "paciente_nombre": paciente.nombre,
                "hospital_destino": hospital_id,
                "cama_destino": cama_asignada.id
            }
        )
    
    print(f"OK: Derivacion aceptada: {paciente.nombre} -> {cama_asignada.id}")
    
    return {
        "mensaje": f"Derivacion aceptada. Paciente asignado a cama {cama_asignada.id}",
        "paciente_id": paciente.id,
        "accion": "aceptado",
        "hospital_actual_id": hospital_id,
        "cama_asignada": jsonable_encoder(cama_asignada),
        "pendiente_egreso_origen": True  # Indica que está pendiente egreso en hospital origen
    }


@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/rechazar-derivacion")
async def rechazar_derivacion(
    hospital_id: str,
    paciente_id: str,
    request_data: RechazarDerivacionRequest,
    session: Session = Depends(get_session)
):
    """
    Rechaza un paciente derivado y lo devuelve al hospital de origen.
    """
    from logic import liberar_sexo_sala
    
    paciente = session.get(Paciente, paciente_id)
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    # ✅ CORRECCIÓN: Verificar que el paciente esté en este hospital (destino)
    if paciente.hospital_id != hospital_id:
        raise HTTPException(
            status_code=400, 
            detail=f"Paciente no pertenece a este hospital. Hospital actual: {paciente.hospital_id}"
        )
    
    if not paciente.derivacion_pendiente:
        raise HTTPException(status_code=400, detail="El paciente no tiene derivación pendiente")
    
    if not paciente.hospital_origen_id:
        raise HTTPException(status_code=400, detail="No se puede determinar el hospital de origen")
    
    hospital_origen_id = paciente.hospital_origen_id
    
    # Guardar motivo de rechazo
    paciente.motivo_rechazo_derivacion = request_data.motivo_rechazo
    
    # ✅ CORRECCIÓN: Devolver al hospital de origen
    paciente.hospital_id = hospital_origen_id
    paciente.derivacion_pendiente = False
    
    # ✅ CORRECCIÓN: Restaurar cama origen
    if paciente.cama_origen_id:
        cama_origen = session.get(Cama, paciente.cama_origen_id)
        if cama_origen:
            # Verificar el estado de la cama
            if cama_origen.estado == EstadoCamaEnum.EN_TRASLADO:
                # La cama estaba reservada, restaurar a OCUPADA
                cama_origen.estado = EstadoCamaEnum.OCUPADA
                cama_origen.paciente_id = paciente.id
                paciente.cama_id = cama_origen.id
                paciente.en_espera = False
                session.add(cama_origen)
                mensaje = f"Derivación rechazada. Paciente devuelto a {cama_origen.id} en hospital de origen. Motivo: {request_data.motivo_rechazo}"
            elif cama_origen.estado == EstadoCamaEnum.LIBRE:
                # La cama fue liberada, ocuparla nuevamente
                cama_origen.estado = EstadoCamaEnum.OCUPADA
                cama_origen.paciente_id = paciente.id
                paciente.cama_id = cama_origen.id
                paciente.en_espera = False
                session.add(cama_origen)
                mensaje = f"Derivación rechazada. Paciente devuelto a {cama_origen.id} (cama estaba libre). Motivo: {request_data.motivo_rechazo}"
            else:
                # La cama fue ocupada por otro paciente
                paciente.cama_id = None
                paciente.en_espera = True
                mensaje = f"Derivación rechazada. Cama original ocupada, paciente en lista de espera. Motivo: {request_data.motivo_rechazo}"
        else:
            # Cama no encontrada
            paciente.cama_id = None
            paciente.en_espera = True
            mensaje = f"Derivación rechazada. Cama original no encontrada, paciente en lista de espera. Motivo: {request_data.motivo_rechazo}"
    else:
        # No tenía cama origen, volver a lista de espera
        paciente.cama_id = None
        paciente.en_espera = True
        mensaje = f"Derivación rechazada. Paciente devuelto a lista de espera del hospital de origen. Motivo: {request_data.motivo_rechazo}"
    
    # Limpiar campos de derivación
    paciente.hospital_origen_id = None
    paciente.cama_origen_id = None
    
    session.add(paciente)
    session.commit()
    session.refresh(paciente)
    
    # Notificar ambos hospitales
    await notificar_cambio(
        hospital_id=hospital_id,
        evento="derivacion_rechazada",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "motivo_rechazo": request_data.motivo_rechazo
        }
    )
    
    await notificar_cambio(
        hospital_id=hospital_origen_id,
        evento="derivacion_rechazada_notificacion",
        session=session,
        detalles={
            "paciente_id": paciente.id,
            "paciente_nombre": paciente.nombre,
            "volvio_a_cama": paciente.cama_id is not None,
            "motivo_rechazo": request_data.motivo_rechazo
        }
    )
    
    print(f"✅ {mensaje}")
    
    return {
        "mensaje": mensaje,
        "paciente_id": paciente.id,
        "accion": "rechazado",
        "hospital_actual_id": hospital_origen_id,
        "en_espera": paciente.en_espera,
        "cama_actual": paciente.cama_id,
        "motivo_rechazo": request_data.motivo_rechazo
    }

@app.post("/hospitales/{hospital_id}/pacientes/{paciente_id}/confirmar-egreso")
async def confirmar_egreso(
    hospital_id: str,
    paciente_id: str,
    session: Session = Depends(get_session)
):
    """
    Confirma el egreso de un paciente derivado, liberando la cama de origen.
    """
    paciente = session.exec(
        select(Paciente).where(
            Paciente.id == paciente_id,
            Paciente.hospital_origen_id == hospital_id
        )
    ).first()
    
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado o no derivado desde este hospital")
    
    if paciente.egreso_confirmado:
        raise HTTPException(status_code=400, detail="El egreso ya fue confirmado")
    
    if not paciente.cama_origen_id:
        raise HTTPException(status_code=400, detail="No hay cama origen para liberar")
    
    cama_origen = session.get(Cama, paciente.cama_origen_id)
    if not cama_origen or cama_origen.hospital_id != hospital_id:
        raise HTTPException(status_code=404, detail="Cama origen no encontrada")
    
    todas_camas = session.exec(select(Cama).where(Cama.hospital_id == hospital_id)).all()
    liberar_sexo_sala(cama_origen, todas_camas, session)
    
    cama_origen.estado = EstadoCamaEnum.LIBRE
    cama_origen.paciente_id = None
    session.add(cama_origen)
    
    paciente.egreso_confirmado = True
    paciente.cama_origen_id = None
    session.add(paciente)
    
    session.commit()
    
    await notificar_cambio(hospital_id, "egreso_confirmado", session, {
        "paciente_id": paciente.id,
        "cama_liberada": cama_origen.id
    })
    
    return {"mensaje": f"Egreso confirmado. Cama {cama_origen.id} liberada", "cama_liberada": cama_origen.id}


# ============================================
# ENDPOINTS - COLA DE PRIORIDAD
# ============================================

@app.get("/hospitales/{hospital_id}/cola-prioridad")
def obtener_cola_prioridad(hospital_id: str, session: Session = Depends(get_session)):
    """
    Obtiene el estado actual de la cola de prioridad del hospital.
    """
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail="Hospital no encontrado")
    
    cola = gestor_colas_global.obtener_cola(hospital_id)
    
    # ✅ CORRECCIÓN: obtener_lista_ordenada ya devuelve diccionarios completos
    # Si se pasa session, incluye toda la info del paciente
    lista_pacientes = cola.obtener_lista_ordenada(session=session)
    
    # ✅ CORRECCIÓN: Agregar explicación de prioridad a cada paciente
    resultado = []
    for info_paciente in lista_pacientes:
        paciente_id = info_paciente["paciente_id"]
        paciente = session.get(Paciente, paciente_id)
        
        if paciente:
            explicacion = explicar_prioridad(paciente)
            
            # Combinar la info base con la explicación
            resultado.append({
                **info_paciente,  # Incluye paciente_id, prioridad, timestamp, posicion, nombre, etc
                "explicacion_prioridad": explicacion
            })
    
    return {
        "hospital_id": hospital_id,
        "total_pacientes": len(resultado),
        "pacientes": resultado
    }


@app.post("/hospitales/{hospital_id}/cola-prioridad/sincronizar")
async def sincronizar_cola(hospital_id: str, session: Session = Depends(get_session)):
    """
    Sincroniza la cola de prioridad con el estado actual de la base de datos.
    """
    hospital = session.get(Hospital, hospital_id)
    if not hospital:
        raise HTTPException(status_code=404, detail="Hospital no encontrado")
    
    num_pacientes = gestor_colas_global.sincronizar_cola_con_db(hospital_id, session)
    
    return {
        "mensaje": "Cola sincronizada",
        "hospital_id": hospital_id,
        "pacientes_en_cola": num_pacientes
    }


@app.get("/hospitales/{hospital_id}/pacientes/{paciente_id}/prioridad")
def obtener_prioridad_paciente(hospital_id: str, paciente_id: str, session: Session = Depends(get_session)):
    """
    Obtiene la explicación detallada de la prioridad de un paciente.
    """
    paciente = session.get(Paciente, paciente_id)
    if not paciente:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    
    if paciente.hospital_id != hospital_id:
        raise HTTPException(status_code=400, detail="Paciente no pertenece al hospital")
    
    return explicar_prioridad(paciente)


# ============================================
# WEBSOCKET
# ============================================

@app.websocket("/ws/{hospital_id}")
async def websocket_endpoint(websocket: WebSocket, hospital_id: str):
    await manager.connect(websocket, hospital_id)
    
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket, hospital_id)
    except:
        manager.disconnect(websocket, hospital_id)


# ============================================
# HEALTH CHECK
# ============================================

@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "redis_available": redis_client is not None,
        "asignacion_automatica_activa": _asignacion_activa,
        "timestamp": datetime.utcnow().isoformat()
    }


# ============================================
# MAIN
# ============================================

if __name__ == "__main__":
    import uvicorn
    
    print("\n" + "="*60)
    print("🏥 SISTEMA DE GESTIÓN DE CAMAS HOSPITALARIAS v2.0")
    print("="*60)
    print("\n📋 CARACTERÍSTICAS:")
    print("   - Cola de prioridad global por hospital")
    print("   - Sistema de asignación automática activa")
    print("   - Soporte multi-hospital")
    print("\n📌 URLs:")
    print("   - Servidor: http://localhost:8000")
    print("   - Dashboard: http://localhost:8000/dashboard")
    print("   - API Docs: http://localhost:8000/docs")
    print("\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")