pipeline {
    agent { label 'selfAgent' }
    stages {
        stage('Build ') {
            steps {
                sh 'docker compose up --build'
            }
        }
    }
}
