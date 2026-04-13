import configparser
import logging
import requests
import time
from typing import Dict, Any

logger = logging.getLogger(__name__)

class WovpClient:
    BASE_URL = "https://worldofvirtualpinball.com/api/whsc/v1/"
    SCORE_PHOTO_URL = f"{BASE_URL}scores/submit-photo"
    SCORE_SUBMIT_URL = f"{BASE_URL}scores/submit"
    
    def __init__(self, config_path: str = "config.ini"):
        self.config = configparser.ConfigParser()
        self.config.read(config_path)
        
        try:
            self.api_key = self.config.get("wovp", "api_key")
            self.challenge_id = self.config.get("wovp", "challenge_id")
        except (configparser.NoSectionError, configparser.NoOptionError) as e:
            logger.warning(f"Configuração do WOVP incompleta no config.ini: {e}")
            self.api_key = ""
            self.challenge_id = ""
            
        self.headers = {
            "X-Client-ID": "vpinleaders-client",
            "Authorization": f"Bearer {self.api_key}"
        }

    def submit(self, screenshot_path: str, score: int, metadata: Dict[str, Any]) -> dict:
        """
        Faz o upload da foto e, em seguida, envia o score usando o ID temporário gerado.
        """
        if not self.api_key or not self.challenge_id:
            raise ValueError("Tentativa de envio ao WOVP sem api_key ou challenge_id configurados.")

        start_time = time.time()
        
        # --- PASSO 1: Upload do Screenshot ---
        try:
            with open(screenshot_path, "rb") as file_stream:
                photo_resp = requests.post(
                    self.SCORE_PHOTO_URL,
                    headers=self.headers,
                    files={"file": file_stream}
                )
        except IOError as e:
            raise Exception(f"Erro ao ler o arquivo de screenshot {screenshot_path}: {e}")

        if photo_resp.status_code != 200:
            raise Exception(f"WOVP Upload falhou com código {photo_resp.status_code}: {photo_resp.text}")
            
        photo_data = photo_resp.json().get("data", {})
        if photo_data.get("errors"):
            raise Exception(f"WOVP retornou erros na imagem: {photo_data.get('errors')}")
            
        photo_temp_id = photo_data.get("photoTempId")
        if not photo_temp_id:
            raise Exception("WOVP não retornou o photoTempId após o upload da imagem.")
        
        # --- PASSO 2: Envio do Score ---
        payload = {
            "metadata": metadata,
            "score": score,
            "playingPlatform": metadata.get("platform", 0),
            "challengeId": self.challenge_id,  # Lendo direto da config
            "photoTempId": photo_temp_id
        }
        
        post_headers = self.headers.copy()
        post_headers["Content-Type"] = "application/json"
        
        score_resp = requests.post(
            self.SCORE_SUBMIT_URL,
            headers=post_headers,
            json=payload
        )
        
        if score_resp.status_code != 200:
            raise Exception(f"WOVP Submit falhou com código {score_resp.status_code}: {score_resp.text}")
            
        duration = int((time.time() - start_time) * 1000)
        logger.info(f"WOVP: Score de {score} submetido com sucesso. Levou {duration}ms.")
        
        return score_resp.json()