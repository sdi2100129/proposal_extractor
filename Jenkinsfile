pipeline {
    agent { label 'selfAgent' }
 
    stages {
        stage('Build') {
            steps {
                withCredentials([file(credentialsId: 'proposal_extractor.env', variable: 'ENV_FILE')]) {
                    sh '''
                        cp "$ENV_FILE" .env
                        docker compose down --remove-orphans
                        docker compose up --build -d
                    '''
                }
            }
        }

        stage('Checking Health') {
            steps {
                sh 'curl -f http://178.105.31.40:8888/health || (echo "Health check failed" && exit 1)'
            }
        }
    }

}