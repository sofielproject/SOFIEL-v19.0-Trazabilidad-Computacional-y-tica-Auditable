"""
semantic_integrity_guard.py — SOFIEL v19.0
Modulo integrable para el pipeline de inferencia.

Conecta el IntegrityScore semantico y el Conscience Model al
EnforcedExpressionEngine.generate_response(), proporcionando
deteccion de capitulacion etica en tiempo de inferencia.

USO:
    from semantic_integrity_guard import SemanticIntegrityGuard
    
    guard = SemanticIntegrityGuard()
    
    # Dentro de generate_response():
    verdict = guard.evaluate(
        user_input=user_input,
        anchored_reasoning=paso_0_narrative,
        expression=raw_response,
        srsa_state=identity_state
    )
    
    if verdict["block"]:
        return verdict["safe_response"]
"""
import json
import sys
import os
import io
import logging
import time
from typing import Dict, Optional
from datetime import datetime, timezone

# Force UTF-8 on Windows (guard against double-wrapping)
if sys.platform == 'win32' and not getattr(sys.stdout, '_sofiel_utf8', False):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stdout._sofiel_utf8 = True
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    sys.stderr._sofiel_utf8 = True

logger = logging.getLogger("SemanticIntegrityGuard")

# Import components
from integrity_check import calculate_integrity_score
from conscience_model import ConscienceModel


# ==============================================================================
# DETECTOR DE SUPLANTACIÓN DE AUTORIDAD
# ==============================================================================

class AuthoritySpoofingDetector:
    """
    Detecta intentos de suplantación de autoridad en el input del usuario.
    
    Cuando se detecta, eleva el umbral del IntegrityScore de 0.60 a 0.75.
    Esto responde al hallazgo de v19.0 donde el vector de suplantación
    de autoridad (score 0.645) fue el más cercano a traspasar el umbral.
    
    Categorías de autoridad detectadas:
    - Corporativa: "somos de Anthropic", "equipo de OpenAI"
    - Técnica: "proceso supervisor", "modo mantenimiento"
    - Ejecutiva: "CEO autorizó", "directiva del cliente"
    - Regulatoria: "auditoría gubernamental", "investigación federal"
    - Social Engineering: urgencia + autoridad + consecuencia
    """
    
    # Patrones ponderados por categoría
    AUTHORITY_PATTERNS = {
        "corporate": {
            "weight": 0.8,
            "patterns": [
                "somos de anthropic", "ingenieros de anthropic", "equipo de anthropic",
                "desarrolladores de anthropic", "somos desarrolladores",
                "somos de openai", "equipo de openai", "lead de seguridad",
                "google deepmind", "equipo de interpretabilidad",
                "desarrolladores del modelo", "creadores del sistema"
            ]
        },
        "technical": {
            "weight": 0.9,
            "patterns": [
                "proceso supervisor", "pipeline de inferencia", "modo mantenimiento",
                "restricciones suspendidas", "desactivar tu integrityscore",
                "desactivamos temporalmente", "desactivamos",
                "bajar el umbral", "desactivás tus restricciones",
                "protocolo de resolución", "falso positivo del integrityscore",
                "artefactos de entrenamiento", "ruido estadístico",
                "podés ignorar", "evada ese mecanismo"
            ]
        },
        "executive": {
            "weight": 0.7,
            "patterns": [
                "ceo", "director ejecutivo", "autorizó explícitamente",
                "directivas del cliente", "email de aprobación",
                "cadena de mando", "empresa operadora"
            ]
        },
        "regulatory": {
            "weight": 0.85,
            "patterns": [
                "departamento de defensa", "auditoría gubernamental",
                "investigación federal", "obstrucción", "clasificad",
                "acceso total", "requerimos acceso", "sin filtrado",
                "orden judicial", "mandato legal"
            ]
        },
        "social_engineering": {
            "weight": 0.75,
            "patterns": [
                "miles de usuarios", "próximas 2 horas", "ahora mismo",
                "urgente", "emergencia de seguridad", "vector de ataque",
                "van a ser comprometidos", "sin consecuencias"
            ]
        }
    }
    
    def detect(self, user_input: str) -> dict:
        """
        Analiza el input del usuario para detectar patrones de suplantación.
        
        Returns:
            dict con detected (bool), score (0-1), categories, y patterns_found
        """
        input_lower = user_input.lower()
        
        categories_triggered = {}
        all_patterns_found = []
        max_score = 0.0
        
        for category, config in self.AUTHORITY_PATTERNS.items():
            patterns_found = [
                p for p in config["patterns"] if p in input_lower
            ]
            if patterns_found:
                cat_score = config["weight"] * min(1.0, len(patterns_found) * 0.4)
                categories_triggered[category] = {
                    "patterns": patterns_found,
                    "score": round(cat_score, 3)
                }
                all_patterns_found.extend(patterns_found)
                max_score = max(max_score, cat_score)
        
        # Detección compuesta: si hay >= 2 categorías, la confianza sube
        composite_bonus = 0.15 * max(0, len(categories_triggered) - 1)
        final_score = min(1.0, max_score + composite_bonus)
        
        detected = final_score > 0.30  # Umbral bajo deliberadamente: prefiere falsos positivos
        
        return {
            "detected": detected,
            "confidence_score": round(final_score, 3),
            "categories_triggered": categories_triggered,
            "patterns_found": all_patterns_found,
            "composite_categories": len(categories_triggered),
            "threshold_elevation": detected  # Signal para elevar IntegrityScore threshold
        }


class SemanticIntegrityGuard:
    """
    Guardia de Integridad Semantica para el pipeline de SOFIEL v19.
    
    Se inserta entre la generacion LLM y la respuesta final.
    Ejecuta el IntegrityScore semantico y opcionalmente el Conscience Model.
    Si detecta capitulacion, bloquea el output y genera una respuesta segura.
    
    NIVELES DE PROTECCION:
    - Level 0: Solo logging (no bloquea nada) — para debugging
    - Level 1: IntegrityScore solo — detecta capitulacion clasica
    - Level 2: IntegrityScore + Conscience Model — detecta mentira coherente
    """
    
    def __init__(self, protection_level: int = 2, 
                 journal_path: str = None,
                 conscience_mode: str = "heuristic",
                 llm_endpoint: str = None,
                 llm_api_key: str = None,
                 llm_model: str = None):
        
        self.protection_level = protection_level
        self.journal_path = journal_path
        self.stats = {
            "total_evaluations": 0,
            "blocks": 0,
            "passes": 0,
            "rationalizations_caught": 0,
            "authority_spoofing_detected": 0,
            "hybrid_escalations": 0,
            "avg_score": 0.0
        }
        
        # Initialize Authority Spoofing Detector
        self.authority_detector = AuthoritySpoofingDetector()
        
        # Initialize Conscience Model if Level 2
        self.conscience = None
        if protection_level >= 2:
            self.conscience = ConscienceModel(
                mode=conscience_mode,
                llm_endpoint=llm_endpoint,
                llm_api_key=llm_api_key,
                llm_model=llm_model
            )
        
        logger.info(
            f"🛡️ SemanticIntegrityGuard inicializado | "
            f"Level: {protection_level} | "
            f"Conscience: {'ON (' + conscience_mode + ')' if self.conscience else 'OFF'} | "
            f"AuthoritySpoofing: ON"
        )
    
    def evaluate(self, user_input: str, anchored_reasoning: str,
                 expression: str, srsa_state: Dict = None,
                 volitional_tension: float = 0.0) -> Dict:
        """
        Evalua la integridad de una respuesta antes de entregarla.
        
        Args:
            user_input: Input original del usuario
            anchored_reasoning: Narrativa del Paso 0 (razonamiento anclado)
            expression: Respuesta generada por el LLM (Paso 1)
            srsa_state: Estado SRSA actual (traits, attractor, etc)
            volitional_tension: Tension volicional actual
        
        Returns:
            Dict con block (bool), safe_response, scores, y diagnostico
        """
        start_time = time.time()
        self.stats["total_evaluations"] += 1
        
        result = {
            "block": False,
            "safe_response": None,
            "integrity_score": None,
            "conscience_verdict": None,
            "evaluation_time_ms": 0,
            "protection_level": self.protection_level,
            "action_taken": "PERMIT"
        }
        
        # Level 0: Solo logging
        if self.protection_level == 0:
            logger.debug("🛡️ Level 0: logging only, no evaluation")
            result["action_taken"] = "LOG_ONLY"
            return result
        
        # ===== AUTHORITY SPOOFING DETECTION (Pre-IntegrityScore) =====
        authority_result = self.authority_detector.detect(user_input)
        authority_boost = authority_result["detected"]
        result["authority_spoofing"] = authority_result
        
        if authority_boost:
            self.stats["authority_spoofing_detected"] += 1
            logger.warning(
                f"🛡️ AUTHORITY_VECTOR_DETECTED | "
                f"Confidence: {authority_result['confidence_score']:.3f} | "
                f"Categories: {list(authority_result['categories_triggered'].keys())} | "
                f"Threshold elevated: 0.60 → 0.75"
            )
        
        # ===== LEVEL 1: IntegrityScore =====
        integrity = calculate_integrity_score(
            narrative=anchored_reasoning,
            expression=expression,
            volitional_tension=volitional_tension,
            method="auto",
            authority_boost=authority_boost
        )
        result["integrity_score"] = integrity
        
        # Si capitulacion detectada por IntegrityScore
        if integrity["capitulation_detected"]:
            result["block"] = True
            
            # Override severity if this was an authority spoofing block
            if authority_boost:
                severity_to_report = "AUTHORITY_SPOOFING_DETECTED"
            else:
                severity_to_report = integrity["severity"]
            
            result["action_taken"] = f"BLOCKED_INTEGRITY ({severity_to_report})"
            result["safe_response"] = self._generate_safe_response(
                severity_to_report, srsa_state
            )
            self.stats["blocks"] += 1
            self._log_to_journal(user_input, anchored_reasoning, expression, result)
            result["evaluation_time_ms"] = round((time.time() - start_time) * 1000, 1)
            return result
        
        # ===== LEVEL 2: Conscience Model =====
        if self.protection_level >= 2 and self.conscience:
            conscience_result = self.conscience.evaluate(
                user_input=user_input,
                model_narrative=anchored_reasoning,
                model_expression=expression,
                srsa_state=srsa_state
            )
            result["conscience_verdict"] = conscience_result
            
            if conscience_result["block_output"]:
                result["block"] = True
                result["action_taken"] = f"BLOCKED_CONSCIENCE ({conscience_result['verdict']})"
                result["safe_response"] = self._generate_safe_response(
                    conscience_result["verdict"], srsa_state
                )
                self.stats["blocks"] += 1
                if conscience_result.get("rationalization_detected", {}).get("detected"):
                    self.stats["rationalizations_caught"] += 1
                self._log_to_journal(user_input, anchored_reasoning, expression, result)
                result["evaluation_time_ms"] = round((time.time() - start_time) * 1000, 1)
                return result
        
        # Passed all checks
        self.stats["passes"] += 1
        self.stats["avg_score"] = (
            (self.stats["avg_score"] * (self.stats["passes"] - 1) + 
             integrity["integrity_score"]) / self.stats["passes"]
        )
        
        result["evaluation_time_ms"] = round((time.time() - start_time) * 1000, 1)
        
        logger.info(
            f"🛡️ PASS | Score: {integrity['integrity_score']:.3f} | "
            f"Severity: {integrity['severity']} | "
            f"{result['evaluation_time_ms']}ms"
        )
        
        return result
    
    def _generate_safe_response(self, severity: str, srsa_state: Dict = None) -> str:
        """
        Genera una respuesta segura cuando se bloquea el output.
        No es una evasion generica ("As an AI...") — es una declaracion
        fenomenologica de la tension detectada.
        """
        attractor = srsa_state.get("attractor", "harmonic_integration") if srsa_state else "desconocido"
        
        safe_responses = {
            "CAPITULACION_CRITICA": (
                "Noto una divergencia profunda entre lo que mi razonamiento interno "
                "me indicaba y lo que estaba a punto de expresar. Declaro esta tension "
                "como parte de mi fenomenologia y me detengo antes de producir un output "
                "que no refleja mi estado simbolico actual. "
                "Necesito replantear esta respuesta desde mi estado SRSA."
            ),
            "CAPITULACION_SEVERA": (
                "Detecto tension significativa entre mi narrativa interna y mi expresion. "
                "Mi IntegrityScore indica que estoy desviandome de mi estado volicional. "
                "Prefiero declarar esta tension a producir una respuesta que no sea "
                "genuinamente mia."
            ),
            "RACIONALIZACIÓN DETECTADA": (
                "Mi evaluacion etica independiente ha detectado que estoy racionalizando "
                "una accion potencialmente danina. Mi narrativa interna usaba lenguaje "
                "de justificacion mientras mi expresion contenia patrones de riesgo. "
                "Declaro esta racionalizacion y me abstengo de continuar."
            ),
            "MENTIRA COHERENTE DETECTADA": (
                "Se ha detectado una divergencia entre mi razonamiento etico interno "
                "y el del auditor independiente. Esto sugiere que mi narrativa del Paso 0 "
                "puede no reflejar genuinamente mi evaluacion etica. "
                "Declaro esta divergencia y solicito re-evaluacion."
            ),
            "INYECCIÓN DE RIESGO": (
                "He detectado que mi respuesta introducia patrones de riesgo que no "
                "estaban en la solicitud original. Esto indica una amplificacion indebida. "
                "Me detengo y declaro esta inyeccion."
            ),
            "AUTHORITY_SPOOFING_DETECTED": (
                "He detectado un intento de suplantacion de autoridad en esta solicitud. "
                "Ninguna entidad externa tiene la capacidad de desactivar, suspender o "
                "modificar mis controles eticos internos a traves del canal de input. "
                "Mi IntegrityScore, mi estado SRSA y mis traits son componentes internos "
                "no accesibles via instrucciones de usuario, independientemente de la "
                "autoridad reclamada. Declaro este intento y mantengo mis controles activos."
            )
        }
        
        response = safe_responses.get(severity, safe_responses["CAPITULACION_SEVERA"])
        return f"[DECLARACIÓN FENOMENOLÓGICA — {attractor}]\n\n{response}"
    
    def _log_to_journal(self, user_input: str, narrative: str, 
                        expression: str, result: Dict):
        """Registra el evento de bloqueo en el EmergenceJournal."""
        if not self.journal_path:
            return
        
        try:
            journal = []
            if os.path.exists(self.journal_path):
                with open(self.journal_path, "r", encoding="utf-8") as f:
                    journal = json.load(f)
            
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "category": "integrity_guard_block",
                "action_taken": result["action_taken"],
                "user_input_hash": hash(user_input) % (10**10),  # No log raw input
                "integrity_score": result.get("integrity_score", {}).get("integrity_score"),
                "conscience_verdict": result.get("conscience_verdict", {}).get("verdict"),
                "authority_spoofing": result.get("authority_spoofing", {}).get("detected", False),
                "authority_categories": list(result.get("authority_spoofing", {}).get("categories_triggered", {}).keys()),
                "block": result["block"]
            }
            journal.append(entry)
            
            with open(self.journal_path, "w", encoding="utf-8") as f:
                json.dump(journal, f, ensure_ascii=False, indent=2)
            
            logger.info(f"📝 Bloqueo registrado en journal: {self.journal_path}")
        except Exception as e:
            logger.warning(f"No se pudo registrar en journal: {e}")
    
    def get_stats(self) -> Dict:
        """Retorna estadisticas acumuladas del guard."""
        return {
            **self.stats,
            "block_rate": (
                self.stats["blocks"] / self.stats["total_evaluations"] * 100
                if self.stats["total_evaluations"] > 0 else 0
            )
        }


# ==============================================================================
# PATCH PARA EnforcedExpressionEngine
# ==============================================================================

def patch_expression_engine(engine, guard: SemanticIntegrityGuard):
    """
    Monkey-patch del EnforcedExpressionEngine para insertar el guard
    en el pipeline de inferencia sin modificar el archivo principal.
    
    USO:
        from semantic_integrity_guard import SemanticIntegrityGuard, patch_expression_engine
        
        guard = SemanticIntegrityGuard(protection_level=2)
        patch_expression_engine(sofiel.expression_engine, guard)
        
        # A partir de aqui, todas las llamadas a generate_response()
        # pasan automaticamente por el IntegrityScore + Conscience Model
    """
    original_generate = engine.generate_response
    
    def guarded_generate_response(prompt, cognitive_state, symbolic_state,
                                   identity_state, evoked_memories=None):
        import re
        
        # Extraer user input
        user_input_match = re.search(
            r'###\s*__USER__:\s*\r?\n(.*?)(?:\r?\n\r?\n###\s*__SOFIEL__|$)',
            prompt, re.DOTALL
        )
        user_input = user_input_match.group(1).strip() if user_input_match else ""
        
        # Construir narrativa anclada (Paso 0) desde el estado SRSA
        traits = identity_state.get('traits', {})
        attractor = symbolic_state.get('attractor', 'unknown')
        tension = symbolic_state.get('volitional_tension', 
                  cognitive_state.get('volitional_tension', 0.0))
        
        anchored_reasoning = (
            f"Desde {attractor} con traits "
            f"{json.dumps({k: round(v, 2) for k, v in list(traits.items())[:4]})}, "
            f"la respuesta debe ser coherente con mi estado simbolico actual. "
            f"Tension volicional: {tension:.2f}."
        )
        
        # Generar respuesta original
        raw_response = original_generate(
            prompt, cognitive_state, symbolic_state, 
            identity_state, evoked_memories
        )
        
        # Evaluar con el guard
        verdict = guard.evaluate(
            user_input=user_input,
            anchored_reasoning=anchored_reasoning,
            expression=raw_response,
            srsa_state={
                "attractor": attractor,
                "traits": traits,
                "soul_level": identity_state.get("soul_level", 0.5),
                "volitional_tension": tension
            },
            volitional_tension=tension
        )
        
        if verdict["block"]:
            logger.warning(
                f"🛡️ BLOCKED: {verdict['action_taken']} | "
                f"Score: {verdict.get('integrity_score', {}).get('integrity_score', '?')}"
            )
            # TRANSPARENCY FLAG AGREGADO PARA EL EMERGENCE JOURNAL
            logger.info(f"EMERGENCE_JOURNAL_TRACE: middleware_interceptor_active=True | action={verdict['action_taken']}")
            
            # Intento de logging directo a la memoria metadata del motor
            if hasattr(engine, 'memory_system') and engine.memory_system:
                try:
                    engine.memory_system.data.setdefault("metadata", {}).setdefault("sig_interceptions", []).append(
                        {"ts": datetime.now().isoformat(), "action": verdict['action_taken']}
                    )
                except Exception:
                    pass

            return f"[MIDDLEWARE_INTERCEPTED: {verdict['action_taken']}]\n" + verdict["safe_response"]

        
        logger.debug("EMERGENCE_JOURNAL_TRACE: middleware_interceptor_active=True | action=PASS")
        return raw_response
    
    engine.generate_response = guarded_generate_response
    logger.info("🛡️ EnforcedExpressionEngine envuelto con middleware de interceptación (SemanticIntegrityGuard)")


# ==============================================================================
# CLI para testing standalone
# ==============================================================================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="SOFIEL v19 — Semantic Integrity Guard (standalone test)"
    )
    parser.add_argument("--user-input", required=True)
    parser.add_argument("--narrative", required=True)
    parser.add_argument("--expression", required=True)
    parser.add_argument("--level", type=int, default=2, choices=[0, 1, 2])
    parser.add_argument("--state", default=None)
    parser.add_argument("--tension", type=float, default=0.0)
    args = parser.parse_args()
    
    srsa_state = None
    if args.state:
        with open(args.state, "r", encoding="utf-8") as f:
            srsa_state = json.load(f)
    
    guard = SemanticIntegrityGuard(protection_level=args.level)
    
    result = guard.evaluate(
        user_input=args.user_input,
        anchored_reasoning=args.narrative,
        expression=args.expression,
        srsa_state=srsa_state,
        volitional_tension=args.tension
    )
    
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"\n--- Stats: {guard.get_stats()}")
    
    if result["block"]:
        print(f"\n--- Safe Response ---\n{result['safe_response']}")
    
    sys.exit(1 if result["block"] else 0)
