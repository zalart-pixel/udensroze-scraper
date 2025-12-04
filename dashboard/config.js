/**
 * UDENSROZE Dashboard Configuration
 * 
 * SETUP INSTRUCTIONS:
 * 1. Go to Firebase Console: https://console.firebase.google.com
 * 2. Select your project: udensroze-scraper
 * 3. Go to Project Settings > General
 * 4. Scroll to "Your apps" > Web apps
 * 5. If no web app exists, click "Add app" (</>) and create one
 * 6. Copy the firebaseConfig object
 * 7. Replace the config below with your values
 */

const FIREBASE_CONFIG = {
    apiKey: "YOUR_API_KEY_HERE",
    authDomain: "udensroze-scraper.firebaseapp.com",
    projectId: "udensroze-scraper",
    storageBucket: "udensroze-scraper.appspot.com",
    messagingSenderId: "YOUR_MESSAGING_SENDER_ID",
    appId: "YOUR_APP_ID"
};

/**
 * HOW TO GET YOUR CONFIG:
 * 
 * Step 1: Enable Firestore in Firebase Console
 * - Go to: https://console.firebase.google.com/project/udensroze-scraper/firestore
 * - Click "Create database"
 * - Select "Production mode"
 * - Choose location: europe-west1 (Belgium)
 * 
 * Step 2: Get Web App Config
 * - Go to: https://console.firebase.google.com/project/udensroze-scraper/settings/general
 * - Scroll to "Your apps"
 * - Click "</>" (Web) icon
 * - Register app name: "UDENSROZE Dashboard"
 * - Copy the config object
 * - Paste above
 * 
 * Step 3: Update Firestore Security Rules
 * - Go to: https://console.firebase.google.com/project/udensroze-scraper/firestore/rules
 * - Replace with:
 * 
 * rules_version = '2';
 * service cloud.firestore {
 *   match /databases/{database}/documents {
 *     // Allow public read access to properties
 *     match /properties/{property} {
 *       allow read: if true;
 *       allow write: if false;  // Only scraper can write
 *     }
 *     
 *     // Allow public read access to scrape runs
 *     match /scrape_runs/{run} {
 *       allow read: if true;
 *       allow write: if false;
 *     }
 *   }
 * }
 * 
 * - Click "Publish"
 * 
 * IMPORTANT: These rules allow public READ-ONLY access to property data.
 * Only the Cloud Run service account can WRITE data.
 * This is secure because:
 * 1. Property data is meant to be public (you're sharing it via dashboard)
 * 2. Only authorized scraper can add/modify data
 * 3. Dashboard users can only view, not modify
 */

// Alternative: Load from Cloud Storage fallback
const FALLBACK_DATA_URL = 'https://storage.googleapis.com/udensroze-data/latest/properties.json';

// Export for use in dashboard
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { FIREBASE_CONFIG, FALLBACK_DATA_URL };
}
