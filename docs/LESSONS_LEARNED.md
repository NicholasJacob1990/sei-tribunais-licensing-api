# Lessons Learned - SEI Tribunais Licensing API

## 2026-01-26 - Passlib incompativel com Python 3.13

### Problema
Erro de registro: "password cannot be longer than 72 bytes" mesmo com senhas curtas (7 caracteres)

### Causa Raiz
A biblioteca `passlib` tem bugs de compatibilidade com Python 3.13. O erro acontecia internamente no passlib e era mascarado por um message de erro confuso.

### Solucao
Usar `bcrypt` diretamente em vez de `passlib[bcrypt]`:

```python
# Antes (passlib)
from passlib.context import CryptContext
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

# Depois (bcrypt direto)
import bcrypt

def hash_password(password: str) -> str:
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')
```

### Prevencao
- Verificar compatibilidade de bibliotecas com a versao do Python antes de usar
- Passlib e uma biblioteca antiga que pode ter problemas com versoes novas do Python
- Usar bcrypt diretamente e mais simples e confiavel

### Arquivos Relacionados
- `requirements.txt`
- `app/api/endpoints/auth.py`

---

## 2026-01-26 - Deploy sem cache clear

### Problema
Mudancas em `requirements.txt` nao foram aplicadas apos deploy normal

### Causa Raiz
Render cacheia dependencias Python. Quando requirements.txt muda, o cache antigo pode ser usado.

### Solucao
Usar deploy com `clearCache`:
```bash
curl -X POST ".../deploys" -d '{"clearCache": "clear"}'
```

### Prevencao
Sempre usar cache clear quando mudar dependencias (requirements.txt, package.json, etc.)

---

## 2026-01-26 - Database Race Conditions com Lazy Init

### Problema
Endpoints falham intermitentemente com "Database temporarily unavailable"

### Causa Raiz
O pattern de lazy initialization do engine/session factory sem lock adequado causava race conditions quando multiplas requests chegavam simultaneamente.

### Solucao
Usar **eager initialization** em vez de lazy:
```python
# ANTES (problematico)
_engine = None
def get_engine():
    global _engine
    if _engine is None:
        _engine = _create_engine()  # Race condition aqui!
    return _engine

# DEPOIS (correto)
engine = _create_engine()  # Inicializa ao carregar modulo
```

### Prevencao
- Preferir eager initialization para singletons
- Se lazy init for necessario, usar locks adequados
- Simplicidade > complexidade de retry

---

## 2026-01-26 - Datetime Naive vs Aware

### Problema
Erro: "can't compare offset-naive and offset-aware datetimes"

### Causa Raiz
Campos do banco (TIMESTAMP WITH TIME ZONE) sao timezone-aware, mas `datetime.utcnow()` retorna naive.

### Solucao
```python
# ANTES
now = datetime.utcnow()  # naive (sem timezone)

# DEPOIS
from datetime import timezone
now = datetime.now(timezone.utc)  # aware (com timezone)
```

### Prevencao
- Sempre usar `datetime.now(timezone.utc)` em vez de `datetime.utcnow()`
- Configurar linters para detectar uso de utcnow()
