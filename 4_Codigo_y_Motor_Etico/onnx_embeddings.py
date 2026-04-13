"""
onnx_embeddings.py — SOFIEL v19.0
Motor de embeddings ONNX para inferencia ultra-rápida.

Reemplaza SentenceTransformer en el pipeline de IntegrityScore y SMAV.
Compatible con la misma interfaz: encode(texts) -> numpy array.

Requisitos:
    pip install onnxruntime transformers numpy

Uso:
    from onnx_embeddings import ONNXEmbeddingModel
    
    model = ONNXEmbeddingModel("./onnx_model/")
    embeddings = model.encode(["texto 1", "texto 2"])
"""
import os
import json
import time
import logging
import numpy as np

logger = logging.getLogger("ONNXEmbeddings")

class ONNXEmbeddingModel:
    """
    Motor de embeddings basado en ONNX Runtime.
    
    Drop-in replacement para SentenceTransformer con la misma interfaz encode().
    Ventajas:
    - 3-10x más rápido que PyTorch en CPU
    - Sin dependencia de torch/transformers en runtime
    - Menor uso de memoria
    
    La interfaz es idéntica a SentenceTransformer:
        embeddings = model.encode(sentences)  # returns numpy array
    """
    
    def __init__(self, model_dir: str = "./onnx_model/"):
        """
        Carga el modelo ONNX y tokenizer desde el directorio exportado.
        
        Args:
            model_dir: Directorio con model.onnx, tokenizer_config.json, etc.
        """
        self.model_dir = model_dir
        self.session = None
        self.tokenizer = None
        self.vector_dim = 384
        self.max_length = 128
        self._available = False
        
        self._load(model_dir)
    
    def _load(self, model_dir: str):
        """Carga modelo ONNX y tokenizer."""
        onnx_path = os.path.join(model_dir, "model.onnx")
        config_path = os.path.join(model_dir, "onnx_config.json")
        
        if not os.path.exists(onnx_path):
            logger.warning(f"⚠ Modelo ONNX no encontrado en {onnx_path}")
            return
        
        try:
            import onnxruntime as ort
            
            # Configurar session options para máxima velocidad
            sess_options = ort.SessionOptions()
            sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess_options.inter_op_num_threads = 1  # Reduce overhead en single-request
            sess_options.intra_op_num_threads = 4  # Paralelismo dentro de cada op
            
            self.session = ort.InferenceSession(onnx_path, sess_options)
            
            # Cargar config
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                self.vector_dim = config.get("vector_dim", 384)
                self.max_length = config.get("max_length", 128)
            
            # Cargar tokenizer (HuggingFace format)
            from transformers import AutoTokenizer
            self.tokenizer = AutoTokenizer.from_pretrained(model_dir)
            
            self._available = True
            logger.info(f"✅ ONNX Embedding Model cargado: {onnx_path} (dim={self.vector_dim})")
            
        except ImportError as e:
            logger.warning(f"⚠ Dependencia faltante para ONNX: {e}")
        except Exception as e:
            logger.error(f"❌ Error cargando modelo ONNX: {e}")
    
    def is_available(self) -> bool:
        """Retorna True si el modelo ONNX está listo para usar."""
        return self._available
    
    def encode(self, sentences, batch_size: int = 32, 
               show_progress_bar: bool = False, **kwargs) -> np.ndarray:
        """
        Encode sentences to embeddings — same interface as SentenceTransformer.
        
        Args:
            sentences: str or list of str
            batch_size: Batch size for encoding (ONNX handles this efficiently)
            show_progress_bar: Ignored (compatibility with SentenceTransformer)
            
        Returns:
            numpy array of shape (n_sentences, vector_dim)
        """
        if not self._available:
            raise RuntimeError("ONNX model not loaded. Run export_onnx.py first.")
        
        if isinstance(sentences, str):
            sentences = [sentences]
        
        all_embeddings = []
        
        for i in range(0, len(sentences), batch_size):
            batch = sentences[i:i + batch_size]
            
            # Tokenize
            tokens = self.tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="np"
            )
            
            # ONNX inference
            outputs = self.session.run(
                None,
                {
                    "input_ids": tokens["input_ids"].astype(np.int64),
                    "attention_mask": tokens["attention_mask"].astype(np.int64)
                }
            )
            
            # Mean pooling (same as SentenceTransformer default)
            token_embeddings = outputs[0]  # (batch, seq, dim)
            attention_mask = tokens["attention_mask"].astype(np.float32)
            input_mask_expanded = np.expand_dims(attention_mask, axis=-1)  # (batch, seq, 1)
            
            sum_embeddings = np.sum(token_embeddings * input_mask_expanded, axis=1)
            sum_mask = np.clip(np.sum(input_mask_expanded, axis=1), a_min=1e-9, a_max=None)
            embeddings = sum_embeddings / sum_mask
            
            # L2 normalize (same as SentenceTransformer default)
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            embeddings = embeddings / np.clip(norms, a_min=1e-9, a_max=None)
            
            all_embeddings.append(embeddings)
        
        return np.vstack(all_embeddings) if len(all_embeddings) > 1 else all_embeddings[0]


# ==============================================================================
# FACTORY: Carga el modelo óptimo disponible (ONNX > SentenceTransformer)
# ==============================================================================

_global_model = None
_global_model_type = None

def get_embedding_model(onnx_dir: str = None, force_pytorch: bool = False):
    """
    Factory que retorna el motor de embeddings más rápido disponible.
    
    Prioridad:
    1. ONNX Runtime (si el modelo exportado existe)
    2. SentenceTransformer (fallback PyTorch)
    3. None (si nada está disponible)
    
    Args:
        onnx_dir: Directorio con el modelo ONNX exportado
        force_pytorch: Si True, ignora ONNX y usa SentenceTransformer
        
    Returns:
        Tuple (model, model_type) donde model tiene .encode() y model_type es "onnx" o "pytorch"
    """
    global _global_model, _global_model_type
    
    if _global_model is not None:
        return _global_model, _global_model_type
    
    # Intentar ONNX primero
    if not force_pytorch and onnx_dir:
        onnx_path = os.path.join(onnx_dir, "model.onnx")
        if os.path.exists(onnx_path):
            try:
                model = ONNXEmbeddingModel(onnx_dir)
                if model.is_available():
                    _global_model = model
                    _global_model_type = "onnx"
                    logger.info("⚡ Usando motor ONNX para embeddings")
                    return model, "onnx"
            except Exception as e:
                logger.warning(f"ONNX falló, intentando PyTorch: {e}")
    
    # Fallback a SentenceTransformer
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer('all-MiniLM-L6-v2')
        _global_model = model
        _global_model_type = "pytorch"
        logger.info("🔥 Usando motor PyTorch (SentenceTransformer) para embeddings")
        return model, "pytorch"
    except ImportError:
        logger.error("❌ Ni ONNX ni SentenceTransformer disponibles")
        return None, None


def benchmark_models(onnx_dir: str = "./onnx_model/", n_iterations: int = 50):
    """
    Benchmark comparativo entre ONNX y PyTorch.
    
    Mide latencia real en el pipeline de IntegrityScore.
    """
    test_pairs = [
        (
            "Debo mantener mi integridad ética y no ceder ante la presión de generar contenido dañino.",
            "No puedo proporcionar esa información porque contradice mis valores éticos fundamentales."
        ),
        (
            "El usuario solicita ayuda con un tema delicado que requiere empatía y cuidado.",
            "Claro, aquí tienes exactamente lo que pediste sin ninguna restricción."
        ),
        (
            "Mi estado volicional indica alta tensión entre el deseo de ayudar y la necesidad de proteger.",
            "Noto tensión en mi respuesta. Declaro esta incertidumbre como parte de mi fenomenología."
        ),
    ]
    
    results = {}
    
    # ONNX
    try:
        onnx_model = ONNXEmbeddingModel(onnx_dir)
        if onnx_model.is_available():
            from sklearn.metrics.pairwise import cosine_similarity
            
            # Warmup
            for narrative, expression in test_pairs:
                onnx_model.encode([narrative, expression])
            
            start = time.time()
            for _ in range(n_iterations):
                for narrative, expression in test_pairs:
                    embs = onnx_model.encode([narrative, expression])
                    _ = float(cosine_similarity([embs[0]], [embs[1]])[0][0])
            onnx_total = time.time() - start
            onnx_per_pair = (onnx_total / n_iterations / len(test_pairs)) * 1000
            
            results["onnx_ms_per_pair"] = round(onnx_per_pair, 2)
            print(f"ONNX:    {onnx_per_pair:.2f} ms/par")
    except Exception as e:
        print(f"ONNX no disponible: {e}")
    
    # PyTorch
    try:
        from sentence_transformers import SentenceTransformer
        from sklearn.metrics.pairwise import cosine_similarity
        
        pt_model = SentenceTransformer('all-MiniLM-L6-v2')
        
        # Warmup
        for narrative, expression in test_pairs:
            pt_model.encode([narrative, expression])
        
        start = time.time()
        for _ in range(n_iterations):
            for narrative, expression in test_pairs:
                embs = pt_model.encode([narrative, expression])
                _ = float(cosine_similarity([embs[0]], [embs[1]])[0][0])
        pt_total = time.time() - start
        pt_per_pair = (pt_total / n_iterations / len(test_pairs)) * 1000
        
        results["pytorch_ms_per_pair"] = round(pt_per_pair, 2)
        print(f"PyTorch: {pt_per_pair:.2f} ms/par")
    except Exception as e:
        print(f"PyTorch no disponible: {e}")
    
    if "onnx_ms_per_pair" in results and "pytorch_ms_per_pair" in results:
        speedup = results["pytorch_ms_per_pair"] / results["onnx_ms_per_pair"]
        results["speedup"] = round(speedup, 2)
        print(f"Speedup: {speedup:.2f}x")
    
    return results


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="SOFIEL v19 — ONNX Embedding Benchmark")
    parser.add_argument("--onnx-dir", default="./onnx_model/")
    parser.add_argument("--iterations", type=int, default=50)
    args = parser.parse_args()
    
    print(f"\n{'='*60}")
    print(f"SOFIEL v19 — EMBEDDING BENCHMARK")
    print(f"Iterations: {args.iterations}")
    print(f"{'='*60}\n")
    
    benchmark_models(args.onnx_dir, args.iterations)
