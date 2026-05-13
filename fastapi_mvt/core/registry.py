"""
App registry and auto-discovery system
Manages app lifecycle and automatic registration
"""

import importlib
import sys
from pathlib import Path
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, FastAPI


class AppConfig:
    """Configuration for a single app"""
    
    def __init__(self, name: str, module_path: str):
        self.name = name
        self.module_path = module_path
        self.router: Optional[APIRouter] = None
        self.models = None
        self.signals = None
        self.tasks = None
    
    def __repr__(self):
        return f"<AppConfig: {self.name}>"


class AppRegistry:
    """
    Central registry for all apps in the project
    Handles auto-discovery and registration
    """
    
    def __init__(self):
        self.apps: Dict[str, AppConfig] = {}
        self._ready = False
    
    def register_app(self, app_path: str):
        """
        Register an app by its module path
        
        Args:
            app_path: Module path like 'apps.blog'
        """
        app_name = app_path.split('.')[-1]
        
        if app_name in self.apps:
            print(f"Warning: App {app_name} already registered")
            return
        
        config = AppConfig(name=app_name, module_path=app_path)
        self.apps[app_name] = config
        
        # Try to import the app module
        try:
            importlib.import_module(app_path)
        except ImportError as e:
            print(f"Warning: Could not import app {app_path}: {e}")
    
    def autodiscover(self, installed_apps: List[str]):
        """
        Auto-discover and register all installed apps
        
        Args:
            installed_apps: List of app module paths
        """
        for app_path in installed_apps:
            self.register_app(app_path)
        
        # Import models from all apps
        for app_name, config in self.apps.items():
            self._import_app_module(config, 'models')
            self._import_app_module(config, 'signals')
            self._import_app_module(config, 'tasks')
        
        self._ready = True
    
    def _import_app_module(self, config: AppConfig, module_name: str):
        """Import a specific module from an app"""
        module_path = f"{config.module_path}.{module_name}"
        try:
            module = importlib.import_module(module_path)
            setattr(config, module_name, module)
        except ImportError:
            # Module doesn't exist, that's okay
            pass
        except Exception as e:
            print(f"Error importing {module_path}: {e}")
    
    def get_routers(self) -> List[APIRouter]:
        """
        Get all routers from registered apps
        
        Returns:
            List of APIRouter instances
        """
        routers = []
        
        for app_name, config in self.apps.items():
            urls_module_path = f"{config.module_path}.urls"
            try:
                urls_module = importlib.import_module(urls_module_path)
                if hasattr(urls_module, 'router'):
                    router = urls_module.router
                    routers.append(router)
                    config.router = router
            except ImportError:
                pass
            except Exception as e:
                print(f"Error loading router from {urls_module_path}: {e}")
        
        return routers
    
    def setup_app(self, app: FastAPI):
        """
        Setup FastAPI app with all registered apps
        
        Args:
            app: FastAPI application instance
        """
        # Include all app routers
        routers = self.get_routers()
        for router in routers:
            app.include_router(router)
    
    def is_ready(self) -> bool:
        """Check if registry is ready"""
        return self._ready
    
    def get_app(self, name: str) -> Optional[AppConfig]:
        """Get app config by name"""
        return self.apps.get(name)
    
    def get_all_apps(self) -> List[AppConfig]:
        """Get all registered apps"""
        return list(self.apps.values())


# Global registry instance
registry = AppRegistry()
