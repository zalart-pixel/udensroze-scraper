#!/usr/bin/env python3
"""
UDENSROZE Real Estate Scraper - Production Version
Integrates: Firestore, Cloud Logging, Error Handling, Monitoring, Alerts
"""

import os
import sys
import json
import time
import random
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from typing import List, Dict, Optional
import re
from collections import defaultdict

# Web scraping
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Error handling
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
from circuitbreaker import circuit

# Google Cloud
from google.cloud import firestore
from google.cloud import storage
from google.cloud import logging as cloud_logging
from google.cloud import secretmanager
from google.auth import default

# ============================================
# CONFIGURATION
# ============================================

CONFIG = {
    'project_id': os.getenv('GCP_PROJECT', 'udensroze-scraper'),
    'firestore_db': os.getenv('FIRESTORE_DB', '(default)'),
    'storage_bucket': os.getenv('STORAGE_BUCKET', 'udensroze-data'),
    
    # Scraping settings
    'test_mode': os.getenv('TEST_MODE', 'false').lower() == 'true',
    'max_properties_per_location': 50 if os.getenv('TEST_MODE', 'false').lower() == 'true' else 50,
    'delay_between_requests': 3,  # seconds
    'delay_between_sites': 5,
    'request_timeout': 15,
    
    # Target locations
    'locations': ['Monopoli', 'Polignano a Mare', 'Fasano', 'Ostuni', 
                  'Savelletri', 'Conversano', 'Carovigno', 'Castellana Grotte',
                  'Alberobello', 'Locorotondo', 'Cisternino', 'Selva di Fasano'],
    
    # Test mode: only Monopoli, 5 properties
    'test_locations': ['Monopoli'],
    'test_max_properties': 5,
    
    # Email alerts
    'smtp_server': 'smtp.gmail.com',
    'smtp_port': 587,
    'smtp_user': os.getenv('SMTP_USER', ''),
    'alert_recipient': os.getenv('ALERT_RECIPIENT', os.getenv('SMTP_USER', '')),
    
    # User agent
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

# ============================================
# LOGGING SETUP
# ============================================

def setup_logging():
    """Setup Cloud Logging"""
    try:
        # Initialize Cloud Logging
        logging_client = cloud_logging.Client()
        logging_client.setup_logging()
        
        # Also log to console for Cloud Run
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s')
        console_handler.setFormatter(formatter)
        
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        logger.addHandler(console_handler)
        
        logging.info("✅ Cloud Logging initialized")
        
    except Exception as e:
        # Fallback to console logging
        logging.basicConfig(
            level=logging.INFO,
            format='[%(asctime)s] %(levelname)s: %(message)s',
            handlers=[logging.StreamHandler(sys.stdout)]
        )
        logging.warning(f"Cloud Logging failed, using console: {e}")

setup_logging()

# ============================================
# GOOGLE CLOUD CLIENTS
# ============================================

class CloudClients:
    """Singleton for Google Cloud clients"""
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance
    
    def _initialize(self):
        """Initialize all clients"""
        try:
            # Get default credentials (service account in Cloud Run)
            credentials, project = default()
            
            self.firestore_client = firestore.Client(
                project=CONFIG['project_id'],
                database=CONFIG['firestore_db']
            )
            
            self.storage_client = storage.Client(project=CONFIG['project_id'])
            
            self.secret_client = secretmanager.SecretManagerServiceClient()
            
            logging.info("✅ Google Cloud clients initialized")
            
        except Exception as e:
            logging.error(f"❌ Failed to initialize Cloud clients: {e}")
            raise

clients = CloudClients()

# ============================================
# EMAIL ALERTS
# ============================================

def get_smtp_password():
    """Get SMTP password from Secret Manager"""
    try:
        name = f"projects/{CONFIG['project_id']}/secrets/gmail-smtp-password/versions/latest"
        response = clients.secret_client.access_secret_version(request={"name": name})
        return response.payload.data.decode('UTF-8')
    except Exception as e:
        logging.warning(f"Could not get SMTP password: {e}")
        return None

def send_alert(subject: str, body: str, alert_type: str = 'info'):
    """Send email alert via Gmail SMTP"""
    
    if not CONFIG['smtp_user'] or not CONFIG['alert_recipient']:
        logging.warning("SMTP not configured, skipping alert")
        return
    
    try:
        password = get_smtp_password()
        if not password:
            logging.warning("No SMTP password, skipping alert")
            return
        
        # Create message
        msg = MIMEMultipart('alternative')
        msg['From'] = CONFIG['smtp_user']
        msg['To'] = CONFIG['alert_recipient']
        msg['Subject'] = subject
        
        # HTML body
        html = f"""
        <html>
          <body style="font-family: Arial, sans-serif;">
            <h2 style="color: {'#dc3545' if alert_type == 'error' else '#28a745'};">
              {'🚨' if alert_type == 'error' else '✅'} {subject}
            </h2>
            <pre style="background: #f5f5f5; padding: 15px; border-radius: 5px;">
{body}
            </pre>
            <p style="color: #666; font-size: 12px;">
              Sent from UDENSROZE Scraper | {datetime.now().strftime('%Y-%m-%d %H:%M:%S EET')}
            </p>
          </body>
        </html>
        """
        
        msg.attach(MIMEText(html, 'html'))
        
        # Send via Gmail
        with smtplib.SMTP(CONFIG['smtp_server'], CONFIG['smtp_port']) as server:
            server.starttls()
            server.login(CONFIG['smtp_user'], password)
            server.send_message(msg)
        
        logging.info(f"📧 Alert sent: {subject}")
        
    except Exception as e:
        logging.error(f"Failed to send alert: {e}")

# ============================================
# HTTP SESSION WITH RETRY
# ============================================

def create_session():
    """Create requests session with retry logic"""
    session = requests.Session()
    
    retry_strategy = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    session.headers.update({'User-Agent': CONFIG['user_agent']})
    
    return session

# ============================================
# PROPERTY EVALUATOR
# ============================================

class PropertyEvaluator:
    """Evaluate properties against UDENSROZE criteria"""
    
    CRITERIA = {
        'geographic': {
            'weight': 0.30,
            'preferred_locations': ['Monopoli', 'Polignano a Mare', 'Fasano', 'Ostuni'],
            'sea_view_critical': True
        },
        'land_space': {
            'weight': 0.25,
            'min_land': 8000,
            'optimal_land': 10000,
            'max_land': 12000
        },
        'architectural': {
            'weight': 0.15,
            'historic_bonus': 15,
            'masseria_bonus': 15
        },
        'infrastructure': {
            'weight': 0.15,
            'base_score': 75
        },
        'regulatory': {
            'weight': 0.10,
            'base_score': 65
        },
        'financial': {
            'weight': 0.05,
            'min_price': 800000,
            'optimal_price': 1250000,
            'max_price': 1500000
        }
    }
    
    @classmethod
    def evaluate(cls, prop: Dict) -> Dict:
        """Evaluate property and add scores"""
        
        # Calculate individual scores
        geo_score = cls._evaluate_geographic(prop)
        land_score = cls._evaluate_land_space(prop)
        arch_score = cls._evaluate_architectural(prop)
        infra_score = cls.CRITERIA['infrastructure']['base_score']
        reg_score = cls.CRITERIA['regulatory']['base_score']
        fin_score = cls._evaluate_financial(prop)
        
        # Calculate weighted total
        total_score = (
            geo_score * cls.CRITERIA['geographic']['weight'] +
            land_score * cls.CRITERIA['land_space']['weight'] +
            arch_score * cls.CRITERIA['architectural']['weight'] +
            infra_score * cls.CRITERIA['infrastructure']['weight'] +
            reg_score * cls.CRITERIA['regulatory']['weight'] +
            fin_score * cls.CRITERIA['financial']['weight']
        )
        
        # Determine priority
        if total_score >= 85:
            priority = 'CRITICAL'
        elif total_score >= 75:
            priority = 'HIGH'
        elif total_score >= 65:
            priority = 'MEDIUM'
        else:
            priority = 'LOW'
        
        # Add scores to property
        prop.update({
            'geographic_score': round(geo_score, 1),
            'land_space_score': round(land_score, 1),
            'architectural_score': round(arch_score, 1),
            'infrastructure_score': round(infra_score, 1),
            'regulatory_score': round(reg_score, 1),
            'financial_score': round(fin_score, 1),
            'total_score': round(total_score, 1),
            'match_percentage': round(total_score, 1),
            'priority': priority,
            'recommendation': cls._generate_recommendation(prop, total_score),
            'strengths': cls._identify_strengths(prop),
            'concerns': cls._identify_concerns(prop)
        })
        
        return prop
    
    @classmethod
    def _evaluate_geographic(cls, prop: Dict) -> float:
        """Evaluate geographic criteria"""
        score = 0.0
        
        # Preferred location
        if prop['location'] in cls.CRITERIA['geographic']['preferred_locations']:
            score += 50
        else:
            score += 20
        
        # Sea view (CRITICAL)
        if prop['sea_view']:
            score += 50
        else:
            score += 0  # Critical failure
        
        return min(score, 100)
    
    @classmethod
    def _evaluate_land_space(cls, prop: Dict) -> float:
        """Evaluate land & space requirements"""
        land = prop['land_area']
        c = cls.CRITERIA['land_space']
        
        if c['min_land'] <= land <= c['max_land']:
            return 100
        elif c['max_land'] < land <= 20000:
            return 85
        elif 6000 <= land < c['min_land']:
            return 60
        else:
            return 40
    
    @classmethod
    def _evaluate_architectural(cls, prop: Dict) -> float:
        """Evaluate architectural feasibility"""
        score = 70
        
        if prop['historic']:
            score += cls.CRITERIA['architectural']['historic_bonus']
        if prop['masseria']:
            score += cls.CRITERIA['architectural']['masseria_bonus']
        
        return min(score, 100)
    
    @classmethod
    def _evaluate_financial(cls, prop: Dict) -> float:
        """Evaluate financial factors"""
        price = prop['price']
        c = cls.CRITERIA['financial']
        
        if c['min_price'] <= price <= c['optimal_price']:
            return 100
        elif c['optimal_price'] < price <= c['max_price']:
            return 80
        elif price < c['min_price']:
            return 70
        else:
            return 50
    
    @classmethod
    def _generate_recommendation(cls, prop: Dict, score: float) -> str:
        """Generate recommendation text"""
        if score >= 85:
            return f"URGENT: Exceptional {prop['property_type']} - schedule site visit within 48 hours"
        elif score >= 75:
            return f"HIGH PRIORITY: Strong candidate - request detailed info"
        elif score >= 65:
            return f"PROMISING: Good potential - gather more data"
        else:
            return f"CONSIDER: Review for specific use cases"
    
    @classmethod
    def _identify_strengths(cls, prop: Dict) -> List[str]:
        """Identify property strengths"""
        strengths = []
        
        if prop['sea_view']:
            strengths.append("Sea view confirmed")
        if prop['masseria']:
            strengths.append(f"Historic masseria {prop['built_area']}m²")
        if prop['historic']:
            strengths.append("Historic structure")
        if 800000 <= prop['price'] <= 1500000:
            strengths.append(f"Price €{prop['price']:,} within budget")
        if 8000 <= prop['land_area'] <= 12000:
            strengths.append(f"Ideal land size {prop['land_area']:,}m²")
        if prop['pool']:
            strengths.append("Existing pool")
        
        return strengths if strengths else ["Property in target region"]
    
    @classmethod
    def _identify_concerns(cls, prop: Dict) -> List[str]:
        """Identify property concerns"""
        concerns = []
        
        if not prop['sea_view']:
            concerns.append("No sea view (critical requirement)")
        if prop['renovation_required']:
            concerns.append("Renovation required")
        if prop['price'] > 1500000:
            concerns.append(f"Over budget by €{prop['price']-1500000:,}")
        if prop['land_area'] < 8000:
            concerns.append(f"Land only {prop['land_area']:,}m² (below minimum)")
        if prop['built_area'] < 400:
            concerns.append(f"Small built area {prop['built_area']}m²")
        
        return concerns if concerns else ["Standard due diligence required"]

# ============================================
# IMMOBILIARE.IT SCRAPER
# ============================================

class ImmobiliareScraper:
    """Scraper for immobiliare.it"""
    
    def __init__(self):
        self.base_url = "https://www.immobiliare.it"
        self.session = create_session()
        self.name = "immobiliare.it"
    
    @circuit(failure_threshold=5, recovery_timeout=300)
    @retry(
        wait=wait_exponential(multiplier=1, min=4, max=60),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type((requests.exceptions.RequestException,))
    )
    def scrape_location(self, location: str) -> List[Dict]:
        """Scrape properties in specific location"""
        properties = []
        
        # Build search URL
        location_slug = location.lower().replace(' ', '-')
        search_url = f"{self.base_url}/vendita-case/{location_slug}/"
        
        try:
            logging.info(f"🔍 Scraping {self.name} for {location}")
            
            # Add random delay to avoid rate limiting
            time.sleep(random.uniform(2, 4))
            
            response = self.session.get(search_url, timeout=CONFIG['request_timeout'])
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Find property listings
            listings = soup.find_all('li', class_='nd-list__item')
            
            if not listings:
                # Try alternative class names
                listings = soup.find_all('div', class_='in-card')
            
            max_props = CONFIG['test_max_properties'] if CONFIG['test_mode'] else CONFIG['max_properties_per_location']
            
            for listing in listings[:max_props]:
                property_data = self._parse_listing(listing, location)
                if property_data:
                    properties.append(property_data)
                
                # Rate limiting
                time.sleep(random.uniform(1, 2))
            
            logging.info(f"   ✅ Found {len(properties)} properties in {location}")
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                logging.warning(f"   ⚠️ Rate limited on {location}, backing off")
                time.sleep(30)
                raise
            else:
                logging.error(f"   ❌ HTTP error for {location}: {e}")
                raise
        
        except Exception as e:
            logging.error(f"   ❌ Error scraping {location}: {e}")
            raise
        
        return properties
    
    def _parse_listing(self, listing, location: str) -> Optional[Dict]:
        """Parse individual listing"""
        try:
            # Extract title
            title_elem = listing.find('a', class_=['nd-list__title', 'in-card__title'])
            if not title_elem:
                return None
            
            title = title_elem.text.strip()
            
            # Extract URL
            url = title_elem.get('href', '')
            if url and not url.startswith('http'):
                url = self.base_url + url
            
            # Extract price
            price_elem = listing.find('li', class_=['nd-list__price', 'in-card__price'])
            price_text = price_elem.text.strip() if price_elem else "0"
            price = self._extract_number(price_text)
            
            # Extract description
            desc_elem = listing.find('div', class_=['nd-list__description', 'in-card__description'])
            description = desc_elem.text.strip() if desc_elem else ""
            
            # Extract features (area, rooms, etc.)
            features_text = ""
            features_elem = listing.find('ul', class_=['nd-list__features', 'in-card__features'])
            if features_elem:
                features_text = features_elem.text.lower()
            
            # Extract areas
            built_area = self._extract_area(features_text, 'mq')
            land_area = self._extract_area(features_text, 'terreno')
            
            # If no land area found, estimate
            if land_area == 0 and built_area > 0:
                land_area = built_area * 20  # Typical ratio in Puglia
            elif land_area == 0:
                land_area = 8000  # Default minimum
            
            # Determine property type
            property_type = self._determine_type(title + " " + description)
            
            # Check for features
            text_content = (title + " " + description + " " + features_text).lower()
            sea_view = any(kw in text_content for kw in ['vista mare', 'sea view', 'vista adriatico', 'vista sul mare'])
            pool = any(kw in text_content for kw in ['piscina', 'pool', 'swimming'])
            historic = any(kw in text_content for kw in ['storica', 'historic', 'antico', 'antica', '1700', '1800'])
            masseria = 'masseria' in text_content
            renovation_required = any(kw in text_content for kw in ['ristruttur', 'renovat', 'da ristrutturare'])
            
            # Create property object
            property_data = {
                'id': str(hash(url) % 10000000),
                'title': title,
                'location': location,
                'price': price,
                'built_area': built_area,
                'land_area': land_area,
                'property_type': property_type,
                'description': description[:500],
                'source': self.name,
                'url': url,
                'discovered_date': datetime.now(timezone.utc).isoformat(),
                'sea_view': sea_view,
                'pool': pool,
                'historic': historic,
                'masseria': masseria,
                'renovation_required': renovation_required,
                'status': 'active',
                'scraped_at': firestore.SERVER_TIMESTAMP
            }
            
            # Only return if meets minimum criteria
            if price >= 500000 and land_area >= 5000:
                return property_data
            
        except Exception as e:
            logging.warning(f"   ⚠️ Error parsing listing: {e}")
        
        return None
    
    def _extract_number(self, text: str) -> int:
        """Extract numeric value from text"""
        numbers = re.findall(r'\d+', text.replace('.', '').replace(',', ''))
        return int(numbers[0]) if numbers else 0
    
    def _extract_area(self, text: str, keyword: str) -> int:
        """Extract area measurement"""
        if keyword in text:
            pattern = r'(\d+(?:[.,]\d+)?)\s*(?:mq|m²|m2|ha|ettari)'
            matches = re.findall(pattern, text)
            if matches:
                value = float(matches[0].replace(',', '.'))
                # Convert hectares to m²
                if 'ha' in text or 'ettari' in text:
                    value *= 10000
                return int(value)
        return 0
    
    def _determine_type(self, text: str) -> str:
        """Determine property type from text"""
        text_lower = text.lower()
        
        if 'masseria' in text_lower:
            return 'masseria'
        elif 'trulli' in text_lower or 'trullo' in text_lower:
            return 'trulli'
        elif 'villa' in text_lower:
            return 'villa'
        elif 'casale' in text_lower:
            return 'casale'
        elif 'agriturismo' in text_lower:
            return 'agriturismo'
        else:
            return 'property'

# ============================================
# MAIN SCRAPER ORCHESTRATOR
# ============================================

class PropertyScraper:
    """Main orchestrator for property scraping"""
    
    def __init__(self):
        self.scrapers = [
            ImmobiliareScraper()
            # Add more scrapers here: IdealitaScraper(), GateAwayScraper()
        ]
        self.evaluator = PropertyEvaluator
        self.run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.results = {
            'run_id': self.run_id,
            'start_time': None,
            'end_time': None,
            'status': 'started',
            'properties_found': 0,
            'critical_count': 0,
            'high_count': 0,
            'successful_sites': [],
            'failed_sites': [],
            'locations_scraped': [],
            'errors': []
        }
    
    def run(self):
        """Run complete scraping operation"""
        
        self.results['start_time'] = datetime.now(timezone.utc)
        
        logging.info("=" * 60)
        logging.info("UDENSROZE PROPERTY SCRAPER - STARTING")
        logging.info("=" * 60)
        
        if CONFIG['test_mode']:
            logging.info("🧪 TEST MODE - Limited scraping")
            locations = CONFIG['test_locations']
        else:
            locations = CONFIG['locations']
        
        logging.info(f"Target: {len(locations)} locations")
        logging.info(f"Sources: {len(self.scrapers)} websites")
        logging.info(f"Run ID: {self.run_id}")
        logging.info("=" * 60)
        
        all_properties = []
        
        # Scrape each location
        for location in locations:
            logging.info(f"\n📍 Processing location: {location}")
            
            for scraper in self.scrapers:
                try:
                    properties = scraper.scrape_location(location)
                    
                    # Evaluate properties
                    for prop in properties:
                        evaluated_prop = self.evaluator.evaluate(prop)
                        all_properties.append(evaluated_prop)
                    
                    if scraper.name not in self.results['successful_sites']:
                        self.results['successful_sites'].append(scraper.name)
                    
                    # Rate limiting between sites
                    time.sleep(CONFIG['delay_between_sites'])
                    
                except Exception as e:
                    logging.error(f"   ❌ Scraper {scraper.name} failed: {e}")
                    self.results['failed_sites'].append({
                        'name': scraper.name,
                        'location': location,
                        'error': str(e),
                        'timestamp': datetime.now(timezone.utc).isoformat()
                    })
                    continue
            
            self.results['locations_scraped'].append(location)
        
        # Remove duplicates
        all_properties = self._remove_duplicates(all_properties)
        
        # Validate data quality
        validation_issues = self._validate_data(all_properties)
        if validation_issues:
            self.results['errors'].extend(validation_issues)
        
        # Calculate statistics
        self.results['properties_found'] = len(all_properties)
        self.results['critical_count'] = sum(1 for p in all_properties if p['priority'] == 'CRITICAL')
        self.results['high_count'] = sum(1 for p in all_properties if p['priority'] == 'HIGH')
        self.results['end_time'] = datetime.now(timezone.utc)
        self.results['status'] = 'completed'
        
        logging.info("\n" + "=" * 60)
        logging.info("✅ SCRAPING COMPLETE")
        logging.info(f"Total properties: {self.results['properties_found']}")
        logging.info(f"CRITICAL: {self.results['critical_count']}")
        logging.info(f"HIGH: {self.results['high_count']}")
        logging.info(f"Duration: {(self.results['end_time'] - self.results['start_time']).seconds // 60} minutes")
        logging.info("=" * 60)
        
        # Save results
        self._save_to_firestore(all_properties)
        self._save_to_cloud_storage(all_properties)
        
        # Send alerts
        self._send_completion_alert(all_properties)
        
        return self.results
    
    def _remove_duplicates(self, properties: List[Dict]) -> List[Dict]:
        """Remove duplicate properties based on URL"""
        seen_urls = set()
        unique_properties = []
        
        for prop in properties:
            if prop['url'] not in seen_urls:
                seen_urls.add(prop['url'])
                unique_properties.append(prop)
        
        removed = len(properties) - len(unique_properties)
        if removed > 0:
            logging.info(f"   🗑️ Removed {removed} duplicate listings")
        
        return unique_properties
    
    def _validate_data(self, properties: List[Dict]) -> List[str]:
        """Validate scraped data quality"""
        issues = []
        
        if len(properties) == 0:
            issues.append("CRITICAL: Zero properties scraped")
            return issues
        
        if len(properties) < 10:
            issues.append(f"WARNING: Only {len(properties)} properties (expected 50+)")
        
        # Check data quality
        missing_price = sum(1 for p in properties if p['price'] == 0)
        if missing_price > len(properties) * 0.5:
            issues.append(f"ERROR: {missing_price} properties missing prices")
        
        missing_location = sum(1 for p in properties if not p.get('location'))
        if missing_location > 0:
            issues.append(f"ERROR: {missing_location} properties missing location")
        
        if issues:
            for issue in issues:
                logging.warning(f"⚠️ {issue}")
        
        return issues
    
    def _save_to_firestore(self, properties: List[Dict]):
        """Save properties to Firestore"""
        try:
            db = clients.firestore_client
            batch = db.batch()
            
            # Save properties
            for prop in properties:
                doc_ref = db.collection('properties').document(prop['id'])
                batch.set(doc_ref, prop, merge=True)
            
            # Save run metadata
            run_ref = db.collection('scrape_runs').document(self.run_id)
            batch.set(run_ref, self.results)
            
            batch.commit()
            
            logging.info(f"💾 Saved {len(properties)} properties to Firestore")
            
        except Exception as e:
            logging.error(f"❌ Failed to save to Firestore: {e}")
            raise
    
    def _save_to_cloud_storage(self, properties: List[Dict]):
        """Save properties to Cloud Storage"""
        try:
            bucket = clients.storage_client.bucket(CONFIG['storage_bucket'])
            
            # Prepare data
            data = {
                'scrape_date': datetime.now(timezone.utc).isoformat(),
                'run_id': self.run_id,
                'total_properties': len(properties),
                'properties': properties,
                'statistics': {
                    'critical': self.results['critical_count'],
                    'high': self.results['high_count'],
                    'medium': sum(1 for p in properties if p['priority'] == 'MEDIUM'),
                    'low': sum(1 for p in properties if p['priority'] == 'LOW'),
                    'avg_price': sum(p['price'] for p in properties) / len(properties) if properties else 0,
                    'avg_match': sum(p['match_percentage'] for p in properties) / len(properties) if properties else 0
                }
            }
            
            # Save to latest/
            blob = bucket.blob('latest/properties.json')
            blob.upload_from_string(json.dumps(data, indent=2), content_type='application/json')
            
            # Save to history/
            timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            blob = bucket.blob(f'history/properties_{timestamp}.json')
            blob.upload_from_string(json.dumps(data, indent=2), content_type='application/json')
            
            logging.info(f"☁️ Saved to Cloud Storage: gs://{CONFIG['storage_bucket']}/latest/properties.json")
            
        except Exception as e:
            logging.error(f"❌ Failed to save to Cloud Storage: {e}")
    
    def _send_completion_alert(self, properties: List[Dict]):
        """Send completion email"""
        
        # Check for critical matches
        critical_props = [p for p in properties if p['priority'] == 'CRITICAL']
        
        # Build alert message
        subject = f"{'🚨 CRITICAL Matches! ' if critical_props else '✅'} UDENSROZE Scrape Complete"
        
        body = f"""
Scrape Run: {self.run_id}
Duration: {(self.results['end_time'] - self.results['start_time']).seconds // 60} minutes

RESULTS:
--------
Total Properties: {len(properties)}
🔴 CRITICAL (85%+): {self.results['critical_count']}
🟠 HIGH (75-84%): {self.results['high_count']}
🟡 MEDIUM (65-74%): {sum(1 for p in properties if p['priority'] == 'MEDIUM')}
⚪ LOW (<65%): {sum(1 for p in properties if p['priority'] == 'LOW')}

Locations Scraped: {', '.join(self.results['locations_scraped'])}
Successful Sites: {', '.join(self.results['successful_sites'])}
Failed Sites: {len(self.results['failed_sites'])}

"""
        
        if critical_props:
            body += "\n🚨 CRITICAL MATCHES:\n" + "=" * 40 + "\n"
            for prop in critical_props[:3]:  # Top 3
                body += f"""
{prop['title']}
Match: {prop['match_percentage']}%
Price: €{prop['price']:,}
Location: {prop['location']}
URL: {prop['url']}
---
"""
        
        if self.results['failed_sites']:
            body += f"\n⚠️ {len(self.results['failed_sites'])} sites failed - check logs\n"
        
        body += f"\nView Dashboard: https://properties.ian.tech\n"
        
        alert_type = 'info' if not critical_props else 'critical'
        send_alert(subject, body, alert_type)

# ============================================
# ENTRY POINT
# ============================================

def main():
    """Main entry point"""
    try:
        logging.info("🚀 UDENSROZE Scraper starting...")
        
        scraper = PropertyScraper()
        results = scraper.run()
        
        # Print top matches
        if results['properties_found'] > 0:
            logging.info("\n🏆 TOP MATCHES:")
            # Would print top properties here
        
        logging.info("\n✅ Scraper completed successfully")
        sys.exit(0)
        
    except KeyboardInterrupt:
        logging.warning("\n⚠️ Scraping interrupted by user")
        sys.exit(1)
        
    except Exception as e:
        logging.error(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        
        # Send error alert
        send_alert(
            "❌ UDENSROZE Scraper Failed",
            f"Error: {e}\n\nCheck Cloud Logging for details",
            'error'
        )
        
        sys.exit(1)

if __name__ == '__main__':
    main()
