"""极地科考站协作基础服务的服务端基础包。"""

from .pharmacy import PharmacyService
from .service import DomainService

__all__ = ["DomainService", "PharmacyService"]
