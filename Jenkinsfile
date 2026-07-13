pipeline {
    agent { label 'selfAgent' }
 
    stages {
        stage('Build') {
            steps {
                withCredentials([file(credentialsId: 'proposal_extractor.env', variable: 'ENV_FILE')]) {
                    sh '''
                        cp "$ENV_FILE" .env
                        docker compose up --build
                    '''
                }
            }
        }
    }

}