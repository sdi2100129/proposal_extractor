pipeline {
    agent any
    stages {
        stage('Static Analysis') {
            steps {
                sh 'make lint'
                sh 'make typecheck'
            }
        }
        stage('Test') {
            steps {
                sh 'make test'
            }
        }
        stage('Build & Push') {
            steps {
                sh 'docker build -t my-proposal-service:${env.BUILD_ID} .'
                sh 'docker push my-proposal-service:${env.BUILD_ID}'
            }
        }
    }
}
